import json
import logging
import types

import httpx
import pytest

from app import assistant, auth, db, main, slicktrip
from tests.test_api import client  # noqa: F401
from tests.test_slicktrip import answer, search


@pytest.fixture
def bridge(monkeypatch):
    """Bridge configured with the new variable names and a login; no real sleeping."""
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password("pw"))
    monkeypatch.setenv("SESSION_SECRET", "s")
    monkeypatch.setenv("BRIDGE_URL", "http://bridge.test:8791/")
    monkeypatch.setenv("BRIDGE_TOKEN", "bridge-secret")
    sleeps = []
    monkeypatch.setattr(assistant.time, "sleep", sleeps.append)
    return sleeps


def script(monkeypatch, *responses):
    """Make httpx.post return/raise the given items in order; returns the list of request bodies."""
    sent, queue = [], list(responses)

    def fake_post(url, headers, json, timeout):
        sent.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(assistant.httpx, "post", fake_post)
    return sent


def ok(answer="hi", session="s1"):
    return httpx.Response(200, json={"answer": answer, "session_id": session})


def test_new_and_legacy_env_names(monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://old/")
    monkeypatch.setenv("APP_URL", "http://old-app/")
    assert assistant.bridge_url() == "http://old" and assistant.app_base_url() == "http://old-app"
    monkeypatch.setenv("BRIDGE_URL", "http://new/")
    monkeypatch.setenv("APP_BASE_URL", "http://new-app/")
    assert assistant.bridge_url() == "http://new" and assistant.app_base_url() == "http://new-app"


def test_request_sends_token_prompt_and_long_timeout(bridge, monkeypatch):
    sent = script(monkeypatch, ok())
    assert assistant.request("price this", "sess") == {"answer": "hi", "session_id": "s1"}
    assert sent[0]["url"] == "http://bridge.test:8791/ask"
    assert sent[0]["headers"] == {"Authorization": "Bearer bridge-secret"}
    assert sent[0]["json"] == {"prompt": "price this", "session_id": "sess"}
    assert sent[0]["timeout"] >= 200


def test_busy_bridge_is_retried_with_backoff(bridge, monkeypatch):
    sent = script(monkeypatch, httpx.Response(429, json={"error": "busy"}), httpx.Response(429, json={}), ok("done"))
    assert assistant.request("q")["answer"] == "done"
    assert len(sent) == 3 and bridge == [3, 6]


def test_busy_bridge_gives_up_with_429(bridge, monkeypatch):
    sent = script(monkeypatch, *[httpx.Response(429, json={"error": "busy"})] * 5)
    with pytest.raises(assistant.AssistantError) as e:
        assistant.request("q")
    assert e.value.status == 429 and "busy" in str(e.value) and len(sent) == 5 and bridge == [3, 6, 12, 24]


@pytest.mark.parametrize("status, text, expected_status, fatal", [
    (400, "prompt must be 1-8000", 502, False), (401, "unauthorized", 502, True), (403, "forbidden", 502, True),
    (502, "claude exploded", 502, False), (504, "claude timed out", 504, False), (500, "", 502, False)])
def test_bridge_errors_have_clear_messages(bridge, monkeypatch, status, text, expected_status, fatal):
    sent = script(monkeypatch, httpx.Response(status, json={"error": text}))
    with pytest.raises(assistant.AssistantError) as e:
        assistant.request("q")
    assert e.value.status == expected_status and e.value.fatal is fatal and len(sent) == 1   # only 429 is retried
    assert "bridge-secret" not in str(e.value)
    if status == 401:
        assert "BRIDGE_TOKEN" in str(e.value)
    if status == 502:
        assert "claude exploded" in str(e.value)


def test_network_failures(bridge, monkeypatch):
    script(monkeypatch, httpx.ConnectError("refused at http://bridge.test"))
    with pytest.raises(assistant.AssistantError, match="Cannot reach Claude bridge: ConnectError"):
        assistant.request("q")
    script(monkeypatch, httpx.ReadTimeout("slow"))
    with pytest.raises(assistant.AssistantError) as e:
        assistant.request("q")
    assert e.value.status == 504


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>"), httpx.Response(200, json=["x"]),
                                      httpx.Response(200, json={"answer": 5}), httpx.Response(200, json={"answer": "a", "session_id": 3})])
def test_garbled_success_reply(bridge, monkeypatch, response):
    script(monkeypatch, response)
    with pytest.raises(assistant.AssistantError, match="could not read"):
        assistant.request("q")


def test_prompt_length_is_checked_before_sending(bridge, monkeypatch):
    sent = script(monkeypatch)
    with pytest.raises(assistant.AssistantError) as e:
        assistant.request("x" * 8001)
    assert e.value.status == 400 and not sent


def test_not_configured_without_login_or_env(monkeypatch):
    with pytest.raises(assistant.AssistantError) as e:
        assistant.request("q")
    assert e.value.status == 503 and e.value.fatal


def test_prompts_and_tokens_are_never_logged(bridge, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    script(monkeypatch, httpx.Response(502, json={"error": "boom"}))
    with pytest.raises(assistant.AssistantError):
        assistant.request("my secret question")
    assert "my secret question" not in caplog.text and "bridge-secret" not in caplog.text


# --- per-question tools token ---------------------------------------------------------------------

def test_tools_token_lifecycle(bridge, monkeypatch):
    token = assistant.mint_token()
    assert assistant.token_valid(token)
    assert not assistant.token_valid("") and not assistant.token_valid(token + "x") and not assistant.token_valid("é")
    assistant.revoke(token)
    assert not assistant.token_valid(token)


def test_tools_token_expires(bridge, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(assistant, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    token = assistant.mint_token()
    clock[0] += assistant.TOKEN_TTL - 1
    assert assistant.token_valid(token)
    clock[0] += 2
    assert not assistant.token_valid(token)


def test_token_is_checked_in_constant_time(monkeypatch):
    calls = []
    real = assistant.hmac.compare_digest
    monkeypatch.setattr(assistant.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    first, _second = assistant.mint_token(), assistant.mint_token()
    assert assistant.token_valid(first)
    assert len(calls) == 2          # compared against every live token, no early exit


def test_ask_mints_a_token_for_the_question_only(bridge, monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://app.test:8000/")
    seen = []

    def fake_post(url, headers, json, timeout):
        seen.append(json)
        assert assistant.token_valid(json["tools_token"])
        return ok()

    monkeypatch.setattr(assistant.httpx, "post", fake_post)
    assistant.ask("hello", "ctx")
    assert seen[0]["tools_url"] == "http://app.test:8000/mcp" and "hello" in seen[0]["prompt"]
    assert not assistant.token_valid(seen[0]["tools_token"])      # revoked once the answer returned


def test_token_is_revoked_even_when_the_bridge_fails(bridge, monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://app.test:8000")
    tokens, real_mint = [], assistant.mint_token
    monkeypatch.setattr(assistant, "mint_token", lambda: tokens.append(real_mint()) or tokens[-1])
    script(monkeypatch, httpx.ConnectError("x"))
    with pytest.raises(assistant.AssistantError):
        assistant.ask("q", "c")
    assert tokens and not assistant.token_valid(tokens[0])


def test_no_tools_without_app_base_url(bridge, monkeypatch):
    sent = script(monkeypatch, ok())
    assistant.ask("q", "c")
    assert "tools_token" not in sent[0]["json"] and "tools_url" not in sent[0]["json"]


# --- SlickTrip refresh through the (mocked) bridge ------------------------------------------------

def test_refresh_prices_each_search_through_the_bridge(bridge, monkeypatch, cfg):
    s1, s2 = search(), search(id="dfw-dad-ow", shape="one-way", legs=[search().legs[0].model_dump()])
    cfg = cfg.model_copy(update={"tracked_searches": [s1, s2]})
    sent = script(monkeypatch, ok(answer()), ok("not json at all"))
    assert slicktrip.refresh(cfg) == {"ok": 1, "errors": 1}
    assert len(sent) == 2 and "dfw-dad-ow" in sent[1]["json"]["prompt"]
    assert "tools_token" not in sent[0]["json"]       # price lookups never get the app's tools
    assert slicktrip.summarize(s1)["total"] == 9950
    bad = slicktrip.summarize(s2)
    assert bad["total"] is None and bad["last_status"] == "invalid"
    assert slicktrip.state["running"] is False


def test_refresh_stops_on_a_bridge_setup_error(bridge, monkeypatch, cfg):
    a, b = search(id="a"), search(id="b")
    cfg = cfg.model_copy(update={"tracked_searches": [a, b]})
    sent = script(monkeypatch, httpx.Response(401, json={}))
    assert slicktrip.refresh(cfg) == {"ok": 0, "errors": 2}
    assert len(sent) == 1
    assert slicktrip.summarize(a)["last_status"] == "error" and "BRIDGE_TOKEN" in slicktrip.summarize(a)["error"]


def test_refresh_continues_after_a_transient_error(bridge, monkeypatch, cfg):
    a, b = search(id="dfw-dad-rt"), search(id="b")
    cfg = cfg.model_copy(update={"tracked_searches": [a, b]})
    script(monkeypatch, httpx.Response(504, json={}), ok(answer(search_id="b")))
    assert slicktrip.refresh(cfg) == {"ok": 1, "errors": 1}


def test_refresh_endpoint_requires_the_bridge(client):
    r = client.post("/api/slicktrip/refresh")
    assert r.status_code == 503 and "BRIDGE_URL" in r.json()["detail"]


def test_refresh_endpoint_rejects_unknown_search(bridge, client):
    client.post("/api/auth/login", json={"password": "pw"})
    assert client.post("/api/slicktrip/refresh?search_id=nope").status_code == 404


def test_prices_endpoint_labels_totals(client, cfg, monkeypatch):
    s = search()
    monkeypatch.setattr(main, "cfg", cfg.model_copy(update={"tracked_searches": [s]}))
    slicktrip.record_answer(s, answer())
    body = client.get("/api/slicktrip").json()
    assert body["price_scope"] == "total" and body["currency"] == "USD"
    row = body["searches"][0]
    assert row["total"] == 9950 and row["travelers"] == 2 and row["cabin"] == "business" and row["editable"] is False
    assert json.dumps(body)         # serializable
    assert db.slicktrip_prices.count_documents({}) == 1
