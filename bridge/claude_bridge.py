#!/usr/bin/env python3
"""Tiny HTTP bridge: runs Claude Code headless on the homelab VM that holds the Slicktrip OAuth login.

POST /ask   {"prompt": "...", "session_id": "optional, to continue a chat"}
            -> {"answer": "...", "session_id": "..."}
GET  /health

Auth: `Authorization: Bearer $BRIDGE_TOKEN` (required; the server refuses to start without it).
Only the Slicktrip MCP tools are pre-approved; in headless mode anything else (Bash, Edit, ...) is denied.
Standard library only, so it needs nothing beyond the `claude` CLI.
"""
import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MCP_SERVER = os.environ.get("MCP_SERVER_NAME", "slicktrip")
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "180"))
MAX_PROMPT = 8000
SLOTS = threading.BoundedSemaphore(int(os.environ.get("BRIDGE_MAX_CONCURRENT", "2")))
SYSTEM = ("You help plan a wedding in Da Nang plus a honeymoon, flying business class from DFW for two. "
          "Use the Slicktrip tools when they help. Be concise. Prices in the user's message come from "
          "their own tracker and are totals for both travelers.")


def build_cmd(prompt: str, session_id: str | None) -> list[str]:
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--allowedTools", f"mcp__{MCP_SERVER}", "--append-system-prompt", SYSTEM]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def ask_claude(prompt: str, session_id: str | None) -> dict:
    proc = subprocess.run(build_cmd(prompt, session_id), capture_output=True, text=True,
                          timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:500] or f"claude exited {proc.returncode}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:500])
    return {"answer": out.get("result", ""), "session_id": out.get("session_id")}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(TOKEN) and hmac.compare_digest(supplied, TOKEN)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ask":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 64_000)) or b"{}")
            prompt = str(body.get("prompt", "")).strip()
            session_id = body.get("session_id") or None
        except (ValueError, TypeError):
            return self._send(400, {"error": "invalid JSON"})
        if not prompt or len(prompt) > MAX_PROMPT:
            return self._send(400, {"error": f"prompt must be 1-{MAX_PROMPT} characters"})
        if not SLOTS.acquire(blocking=False):
            return self._send(429, {"error": "bridge busy, try again shortly"})
        try:
            self._send(200, ask_claude(prompt, session_id))
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "claude timed out"})
        except (RuntimeError, ValueError) as e:
            self._send(502, {"error": str(e)})
        finally:
            SLOTS.release()

    def log_message(self, fmt, *args):  # keep prompts out of logs
        print(f"{self.address_string()} {args[0] if args else ''}")


def main() -> None:
    if not TOKEN:
        raise SystemExit("BRIDGE_TOKEN is required")
    host, port = os.environ.get("BRIDGE_HOST", "0.0.0.0"), int(os.environ.get("BRIDGE_PORT", "8787"))
    print(f"claude bridge listening on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
