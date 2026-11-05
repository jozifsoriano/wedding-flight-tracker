"""Client for the Claude Code bridge (see bridge/). Settings come from the environment, never config.yaml."""
import os
import secrets
import threading
import time

import httpx

TIMEOUT = 200.0   # the bridge allows claude 180s


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


TOKEN_TTL = 600
_tokens: dict[str, float] = {}
_lock = threading.Lock()


def tools_enabled() -> bool:
    """Claude may manage destinations only when the bridge can reach this app (APP_URL)."""
    return enabled() and bool(os.environ.get("APP_URL"))


def mint_token() -> str:
    """Single-question bearer token for the /mcp endpoint; revoked when the question ends."""
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.monotonic()
        for t in [t for t, exp in _tokens.items() if exp < now]:
            del _tokens[t]
        _tokens[token] = now + TOKEN_TTL
    return token


def token_valid(token: str) -> bool:
    with _lock:
        exp = _tokens.get(token)
        return exp is not None and exp >= time.monotonic()


def revoke(token: str) -> None:
    with _lock:
        _tokens.pop(token, None)


def enabled() -> bool:
    return bool(os.environ.get("CLAUDE_BRIDGE_URL") and os.environ.get("CLAUDE_BRIDGE_TOKEN"))


def trip_context(best_trips: list[dict], limit: int = 6) -> str:
    lines = [f"- {p['destination']} ({p['type']}): ${p['total']:,.0f} total for both travelers"
             for p in best_trips if p.get("total") is not None][:limit]
    return "Cheapest trips currently tracked:\n" + "\n".join(lines) if lines else "No complete trip prices tracked yet."


def ask(question: str, context: str, session_id: str | None = None) -> dict:
    if not enabled():
        raise AssistantError("Claude bridge is not configured (set CLAUDE_BRIDGE_URL and CLAUDE_BRIDGE_TOKEN)", 503)
    url = os.environ["CLAUDE_BRIDGE_URL"].rstrip("/") + "/ask"
    headers = {"Authorization": f"Bearer {os.environ['CLAUDE_BRIDGE_TOKEN']}"}
    body = {"prompt": f"{context}\n\nQuestion: {question}", "session_id": session_id}
    token = None
    if tools_enabled():
        token = mint_token()
        body.update(tools_url=os.environ["APP_URL"].rstrip("/") + "/mcp", tools_token=token)
    try:
        r = httpx.post(url, headers=headers, timeout=TIMEOUT, json=body)
    except httpx.HTTPError as e:
        raise AssistantError(f"Cannot reach Claude bridge: {e.__class__.__name__}") from e
    finally:
        if token:
            revoke(token)
    if r.status_code != 200:
        try:
            detail = r.json().get("error", "")
        except ValueError:
            detail = ""
        raise AssistantError(f"Bridge error ({r.status_code}): {detail}".strip(), 429 if r.status_code == 429 else 502)
    reply = r.json()
    return {"answer": reply.get("answer", ""), "session_id": reply.get("session_id")}
