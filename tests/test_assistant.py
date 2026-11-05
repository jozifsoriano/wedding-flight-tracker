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


def test_disabled_without_env(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_BRIDGE_URL", raising=False)
    assert client.get("/api/assistant").json() == {"enabled": False}
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 503


def test_ask_forwards_to_bridge(client, monkeypatch):
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


def test_bridge_failure_is_502(client, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://bridge:8787")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN", "tok")
    def boom(*a, **k): raise httpx.ConnectError("down")
    monkeypatch.setattr(assistant.httpx, "post", boom)
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 502


def test_bridge_command_only_allows_slicktrip_tools():
    cmd = claude_bridge.build_cmd("hello", "abc")
    assert cmd[:3] == ["claude", "-p", "hello"]
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__slicktrip"
    assert cmd[cmd.index("--resume") + 1] == "abc"


def test_bridge_parses_claude_json(monkeypatch):
    out = json.dumps({"result": "ok", "session_id": "s9", "is_error": False})
    monkeypatch.setattr(claude_bridge.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert claude_bridge.ask_claude("q", None) == {"answer": "ok", "session_id": "s9"}
