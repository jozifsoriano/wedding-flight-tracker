import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app import assistant, main

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
import claude_bridge  # noqa: E402

from tests.test_api import client  # noqa: E402,F401


PASSWORD = "correct horse battery"


@pytest.fixture
def logged_in(client, monkeypatch):
    """Turn the login on and sign the test client in."""
    from app import auth
    monkeypatch.setenv("APP_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    return client


def test_claude_stays_off_without_a_login(client, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8791")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    monkeypatch.setenv("APP_URL", "http://app:8000")
    assert client.get("/api/assistant").json() == {"enabled": False, "can_manage": False}
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 503


def test_disabled_without_env(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_BRIDGE_URL", raising=False)
    assert client.get("/api/assistant").json() == {"enabled": False, "can_manage": False}
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 503


def test_ask_forwards_to_bridge(logged_in, client, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8787/")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)
        return httpx.Response(200, json={"answer": "yes", "session_id": "s1"})

    monkeypatch.setattr(assistant.httpx, "post", fake_post)
    r = client.post("/api/ask", json={"question": "good deal?", "session_id": "s0"})
    assert r.json() == {"answer": "yes", "session_id": "s1"}
    assert seen["url"] == "http://bridge:8787/ask" and seen["headers"]["Authorization"] == "Bearer tok"
    assert "good deal?" in seen["json"]["prompt"] and seen["json"]["session_id"] == "s0"


def test_bridge_failure_is_502(logged_in, client, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8787")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    def boom(*a, **k): raise httpx.ConnectError("down")
    monkeypatch.setattr(assistant.httpx, "post", boom)
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 502


def test_bridge_command_only_allows_slicktrip_tools():
    cmd = claude_bridge.build_cmd("abc")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__slicktrip"
    assert cmd[cmd.index("--resume") + 1] == "abc"


def test_bridge_parses_claude_json(monkeypatch):
    out = json.dumps({"result": "ok", "session_id": "s9", "is_error": False})
    monkeypatch.setattr(claude_bridge.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert claude_bridge.ask_claude("q", None) == {"answer": "ok", "session_id": "s9"}


# --- Claude's tools: /mcp endpoint, approvals ---------------------------------------------------

import yaml  # noqa: E402

from app import actions, mcp  # noqa: E402


@pytest.fixture
def tools_client(logged_in, client, monkeypatch, cfg, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.delenv("GUI_DESTINATIONS_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8791")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    token = assistant.mint_token()
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def rpc(client, method, params=None, id=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})
    return r


def call(client, name, args=None):
    body = rpc(client, "tools/call", {"name": name, "arguments": args or {}}).json()["result"]
    return body["isError"], body["content"][0]["text"]


SEOUL = {"name": "Seoul", "country": "South Korea", "airports": ["icn"],
         "start_date": "2027-03-15", "end_date": "2027-03-19"}


def test_mcp_requires_valid_token(logged_in, client):
    client.cookies.clear()   # the tool endpoint must work on its token alone, and need no session
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/mcp", json=msg).status_code == 401
    assert client.post("/mcp", json=msg, headers={"Authorization": "Bearer nope"}).status_code == 401
    token = assistant.mint_token()
    assert client.post("/mcp", json=msg, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assistant.revoke(token)
    assert client.post("/mcp", json=msg, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_mcp_handshake_and_tool_list(tools_client):
    init = rpc(tools_client, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    note = tools_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    names = {t["name"] for t in rpc(tools_client, "tools/list").json()["result"]["tools"]}
    assert names == {"list_destinations", "get_best_trips", "add_destination", "update_destination",
                     "request_remove_destination", "refresh_prices"}
    assert rpc(tools_client, "nope").json()["error"]["code"] == -32601
    assert "error" in rpc(tools_client, "tools/call", {"name": "rm_rf"}).json()


def test_claude_can_add_and_update_but_not_edit_config_destinations(tools_client):
    err, text = call(tools_client, "add_destination", SEOUL)
    assert not err and "seoul" in text
    assert main.cfg.honeymoon[-1].airports == ["ICN"]

    err, text = call(tools_client, "update_destination", {"id": "seoul", "end_date": "2027-03-21"})
    assert not err and main.cfg.honeymoon[-1].end_dates == ["2027-03-21"]

    err, text = call(tools_client, "update_destination", {"id": "tokyo", "end_date": "2027-03-21"})
    assert err and "Move built-in destinations" in text    # tokyo comes from config.yaml until migrated
    err, _ = call(tools_client, "update_destination", {"id": "seoul", "end_date": "2027-03-01"})
    assert err                               # before start date
    err, text = call(tools_client, "add_destination", SEOUL)
    assert err and "already" in text

    listing = call(tools_client, "list_destinations")[1]
    assert '"editable": true' in listing and '"editable": false' in listing
    kinds = [a["kind"] for a in db_actions()]
    assert kinds.count("add_destination") == 1 and kinds.count("update_destination") == 1


def db_actions():
    from app import db
    return list(db.actions.find({}))


def test_removal_needs_user_approval(tools_client):
    call(tools_client, "add_destination", SEOUL)
    err, text = call(tools_client, "request_remove_destination", {"id": "seoul"})
    assert not err and "NOT been removed" in text
    assert any(d.id == "seoul" for d in main.cfg.honeymoon)           # still there
    call(tools_client, "request_remove_destination", {"id": "seoul"})  # asking twice must not double-queue

    pending = tools_client.get("/api/actions", headers={}).json()["pending"]
    assert len(pending) == 1 and pending[0]["summary"] == "Remove Seoul"

    assert tools_client.post(f"/api/actions/{pending[0]['id']}/approve").status_code == 200
    assert [d.id for d in main.cfg.honeymoon] == ["tokyo"]
    assert tools_client.post(f"/api/actions/{pending[0]['id']}/approve").status_code == 404   # one-shot
    assert tools_client.get("/api/actions").json()["recent"][0]["status"] == "applied"


def test_rejected_removal_keeps_destination(tools_client):
    call(tools_client, "add_destination", SEOUL)
    call(tools_client, "request_remove_destination", {"id": "seoul"})
    action_id = tools_client.get("/api/actions").json()["pending"][0]["id"]
    assert tools_client.post(f"/api/actions/{action_id}/reject").status_code == 200
    assert any(d.id == "seoul" for d in main.cfg.honeymoon)


def test_config_destinations_cannot_be_removed(tools_client):
    err, text = call(tools_client, "request_remove_destination", {"id": "tokyo"})
    assert err and not actions.list_recent()["pending"]


def test_app_destination_limit(tools_client, monkeypatch):
    monkeypatch.setattr(main, "MAX_APP_DESTINATIONS", 1)
    assert not call(tools_client, "add_destination", SEOUL)[0]
    err, text = call(tools_client, "add_destination", {**SEOUL, "name": "Osaka", "airports": ["KIX"]})
    assert err and "Limit" in text


def test_ask_attaches_tools_only_when_app_url_set(logged_in, client, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8791")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    sent = []
    def fake_post(url, headers, json, timeout):
        sent.append(json)
        if "tools_token" in json:
            assert assistant.token_valid(json["tools_token"])
        return httpx.Response(200, json={"answer": "ok", "session_id": None})
    monkeypatch.setattr(assistant.httpx, "post", fake_post)

    client.post("/api/ask", json={"question": "hi"})
    assert "tools_token" not in sent[0]
    monkeypatch.setenv("APP_URL", "http://app:8000/")
    client.post("/api/ask", json={"question": "hi"})
    assert sent[1]["tools_url"] == "http://app:8000/mcp"
    assert not assistant.token_valid(sent[1]["tools_token"])      # revoked once the question ended


# --- bridge hardening ---------------------------------------------------------------------------

def test_bridge_command_has_no_builtin_tools_and_prompt_not_in_argv():
    cmd = claude_bridge.build_cmd("abc", "/tmp/x.json")
    assert "-p" in cmd and "hello" not in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__slicktrip,mcp__wedding"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--resume") + 1] == "abc" and cmd[cmd.index("--mcp-config") + 1] == "/tmp/x.json"
    assert "mcp__wedding" not in claude_bridge.build_cmd(None)[claude_bridge.build_cmd(None).index("--allowedTools") + 1]


def test_bridge_only_attaches_tools_for_allowed_app_url(monkeypatch):
    monkeypatch.setattr(claude_bridge, "ALLOWED_APP_URL", "http://app:8000")
    assert claude_bridge.tools_allowed("http://app:8000/mcp")
    assert not claude_bridge.tools_allowed("http://evil:8000/mcp")
    assert not claude_bridge.tools_allowed("http://app:8000.evil.test/mcp")
    monkeypatch.setattr(claude_bridge, "ALLOWED_APP_URL", "")
    assert not claude_bridge.tools_allowed("http://app:8000/mcp")


def test_bridge_scrubs_env_and_keeps_token_off_argv(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "secret-bridge-token")
    monkeypatch.setenv("MONGO_URI", "mongodb://x")
    monkeypatch.setattr(claude_bridge, "ALLOWED_APP_URL", "http://app:8000")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        cfg_path = cmd[cmd.index("--mcp-config") + 1]
        seen["config"] = json.load(open(cfg_path))
        seen["mode"] = oct(Path(cfg_path).stat().st_mode & 0o777)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok", "session_id": "s"}), "")

    monkeypatch.setattr(claude_bridge.subprocess, "run", fake_run)
    claude_bridge.ask_claude("hello", None, "http://app:8000/mcp", "run-token")
    assert "BRIDGE_TOKEN" not in seen["env"] and "MONGO_URI" not in seen["env"]
    assert "run-token" not in " ".join(seen["cmd"]) and seen["input"] == "hello"
    assert seen["config"]["mcpServers"]["wedding"]["headers"]["Authorization"] == "Bearer run-token"
    assert seen["mode"] == "0o600"
