import json

import httpx
import pytest
import yaml

from app import actions, assistant, db, main, slicktrip
from tests.test_api import client  # noqa: F401
from tests.test_assistant import call, logged_in, rpc, tools_client  # noqa: F401
from tests.test_slicktrip import answer, search

OPEN_JAW = {"name": "Open jaw Tokyo", "shape": "multi-city", "cabin": "business", "travelers": 2, "legs": [
    {"origin": "DFW", "destination": "DAD", "date": "2027-03-05"},
    {"origin": "TYOA", "destination": "DFW", "date": "2027-03-19"}]}


@pytest.fixture
def seeded(tools_client, monkeypatch):
    """A config.yaml search (read-only) next to the app-managed ones."""
    cfg = main.cfg.model_copy(update={"tracked_searches": [search()]})
    monkeypatch.setattr(main, "cfg", cfg)
    main.CONFIG_PATH.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))   # reloads must keep it
    monkeypatch.setenv("GUI_SEARCHES_PATH", str(main.CONFIG_PATH.parent / "user_searches.yaml"))
    return tools_client


def test_tool_list_is_unchanged(seeded):
    names = {t["name"] for t in rpc(seeded, "tools/list").json()["result"]["tools"]}
    assert names == {"list_destinations", "get_best_trips", "add_destination", "update_destination",
                     "request_remove_destination", "refresh_prices"}


def test_claude_can_add_an_open_jaw_search(seeded):
    err, text = call(seeded, "add_destination", OPEN_JAW)
    assert not err, text
    added = next(t for t in main.cfg.tracked_searches if t.id == "open-jaw-tokyo")
    assert added.shape == "multi-city" and added.cabin == "business" and added.travelers == 2
    assert [l.origin for l in added.legs] == ["DFW", "TYOA"]
    assert any(a["kind"] == "add_search" for a in db.actions.find({}))
    err, text = call(seeded, "add_destination", OPEN_JAW)
    assert err and "already" in text


def test_search_defaults_to_business_for_two(seeded):
    minimal = {"name": "One way", "shape": "one-way", "legs": [{"origin": "DFW", "destination": "DAD", "date": "2027-03-05"}]}
    assert not call(seeded, "add_destination", minimal)[0]
    added = next(t for t in main.cfg.tracked_searches if t.id == "one-way")
    assert (added.cabin, added.travelers) == ("business", 2)


@pytest.mark.parametrize("bad", [
    {"shape": "round-trip"},                                   # round trip needs a reverse return leg
    {"legs": [{"origin": "DFW", "destination": "DAD", "date": "not-a-date"}]},
    {"travelers": 99}, {"cabin": "steerage"},
])
def test_invalid_searches_are_refused(seeded, bad):
    err, text = call(seeded, "add_destination", {**OPEN_JAW, **bad})
    assert err and "invalid" in text.lower()
    assert [t.id for t in main.cfg.tracked_searches] == ["dfw-dad-rt"]


def test_config_searches_are_read_only(seeded):
    err, text = call(seeded, "update_destination", {"id": "dfw-dad-rt", "travelers": 4})
    assert err and "read-only" in text
    assert main.cfg.tracked_searches[0].travelers == 2
    err, text = call(seeded, "request_remove_destination", {"id": "dfw-dad-rt"})
    assert err and "read-only" in text and not actions.list_recent()["pending"]


def test_app_searches_can_be_updated(seeded):
    call(seeded, "add_destination", OPEN_JAW)
    err, text = call(seeded, "update_destination", {"id": "open-jaw-tokyo", "cabin": "economy", "travelers": 3})
    assert not err, text
    updated = next(t for t in main.cfg.tracked_searches if t.id == "open-jaw-tokyo")
    assert (updated.cabin, updated.travelers) == ("economy", 3)
    assert call(seeded, "update_destination", {"id": "open-jaw-tokyo", "legs": []})[0]


def test_search_removal_only_queues_until_approved(seeded):
    call(seeded, "add_destination", OPEN_JAW)
    err, text = call(seeded, "request_remove_destination", {"id": "open-jaw-tokyo"})
    assert not err and "NOT been removed" in text
    call(seeded, "request_remove_destination", {"id": "open-jaw-tokyo"})       # no duplicate request
    assert any(t.id == "open-jaw-tokyo" for t in main.cfg.tracked_searches)
    pending = seeded.get("/api/actions").json()["pending"]
    assert len(pending) == 1
    assert seeded.post(f"/api/actions/{pending[0]['id']}/approve").status_code == 200
    assert [t.id for t in main.cfg.tracked_searches] == ["dfw-dad-rt"]


def test_denied_search_removal_keeps_it(seeded):
    call(seeded, "add_destination", OPEN_JAW)
    call(seeded, "request_remove_destination", {"id": "open-jaw-tokyo"})
    action_id = seeded.get("/api/actions").json()["pending"][0]["id"]
    assert seeded.post(f"/api/actions/{action_id}/reject").status_code == 200
    assert any(t.id == "open-jaw-tokyo" for t in main.cfg.tracked_searches)


def test_the_only_removal_tool_is_a_request():
    removal_tools = [name for name in main.TOOLS if "remove" in name or "delete" in name]
    assert removal_tools == ["request_remove_destination"]


def test_listing_shows_prices_and_editability(seeded):
    slicktrip.record_answer(search(), answer())
    err, text = call(seeded, "list_destinations")
    data = json.loads(text)
    row = data["slicktrip_searches"][0]
    assert not err and row["id"] == "dfw-dad-rt" and row["editable"] is False and row["total"] == 9950
    assert "not per person" in data["note"] and data["destinations"][0]["id"] == "tokyo"


def test_refresh_prices_source(seeded, monkeypatch):
    started = []
    monkeypatch.setattr(main, "_start_refresh", lambda: started.append("google") or {"started": True})
    monkeypatch.setattr(main, "_start_slicktrip_refresh", lambda: started.append("slicktrip") or {"started": True})
    call(seeded, "refresh_prices")
    call(seeded, "refresh_prices", {"source": "slicktrip"})
    assert started == ["google", "slicktrip"]


# --- the /mcp endpoint's limits -------------------------------------------------------------------

def test_mcp_rejects_oversized_bodies(tools_client):
    big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (main.MAX_MCP_BODY + 1)}}
    assert tools_client.post("/mcp", json=big).status_code == 413


def test_mcp_rejects_oversized_streamed_bodies(tools_client):
    def chunks():
        for _ in range(3):
            yield b"x" * (main.MAX_MCP_BODY // 2)
    assert tools_client.post("/mcp", content=chunks()).status_code == 413


def test_mcp_rejects_garbage(tools_client):
    assert tools_client.post("/mcp", content=b"{nope").status_code == 400
    assert tools_client.post("/mcp", content=b"[1]").json()["error"]["code"] == -32600


def test_token_is_checked_before_the_body_is_read(client):
    huge = b"x" * (main.MAX_MCP_BODY + 10)
    assert client.post("/mcp", content=huge).status_code == 401


def test_tools_token_stops_working_after_the_question(logged_in, client, monkeypatch):
    monkeypatch.setenv("BRIDGE_URL", "http://bridge.test")
    monkeypatch.setenv("BRIDGE_TOKEN", "t")
    monkeypatch.setenv("APP_BASE_URL", "http://app.test")
    seen = {}

    def fake_post(url, headers, json, timeout):
        client.headers["Authorization"] = f"Bearer {json['tools_token']}"
        seen["during"] = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code
        seen["token"] = json["tools_token"]
        return httpx.Response(200, json={"answer": "ok", "session_id": None})

    monkeypatch.setattr(assistant.httpx, "post", fake_post)
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 200
    after = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert seen["during"] == 200 and after.status_code == 401
