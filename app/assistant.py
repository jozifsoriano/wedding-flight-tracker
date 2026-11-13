"""Client for the Claude Code bridge (see bridge/). Settings come from the environment, never config.yaml.

Neither prompts nor tokens are ever logged, and error messages never include them."""
import hmac
import os
import secrets
import threading
import time

import httpx

from . import auth

TIMEOUT = 200.0                   # the bridge allows claude 180s
MAX_PROMPT = 8000                 # the bridge rejects longer prompts
BUSY_BACKOFF = (3, 6, 12, 24)     # seconds to wait after each 429 (bridge handles one request at a time)
TOKEN_TTL = 300                   # worst case: all backoff (45s) + the 200s timeout, with margin

_tokens: dict[str, float] = {}
_lock = threading.Lock()
_bridge_lock = threading.Lock()   # this app sends one bridge request at a time (the bridge is single-slot)


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 502, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.fatal = fatal   # a setup problem (token, allowlist, not configured): retrying other requests won't help


def _env(*names: str) -> str:
    """First non-empty variable; the CLAUDE_BRIDGE_*/APP_URL names are the older spellings."""
    return next((os.environ[n].strip() for n in names if os.environ.get(n, "").strip()), "")


def bridge_url() -> str:
    return _env("BRIDGE_URL", "CLAUDE_BRIDGE_URL").rstrip("/")


def bridge_token() -> str:
    return _env("BRIDGE_TOKEN", "CLAUDE_BRIDGE_TOKEN")


def app_base_url() -> str:
    return _env("APP_BASE_URL", "APP_URL").rstrip("/")


def enabled() -> bool:
    """Claude features stay off until the app has a login (they can spend the Claude plan and change data)."""
    return auth.enabled() and bool(bridge_url() and bridge_token())


def tools_enabled() -> bool:
    """Claude may manage destinations only when the bridge can reach this app (APP_BASE_URL)."""
    return enabled() and bool(app_base_url())


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
    """Constant-time check against every live token (no early exit, no dict lookup on the secret)."""
    supplied = token.encode("utf-8", "replace")
    valid = False
    with _lock:
        now = time.monotonic()
        for known, exp in _tokens.items():
            if hmac.compare_digest(known.encode(), supplied) and exp >= now:
                valid = True
    return valid


def revoke(token: str) -> None:
    with _lock:
        _tokens.pop(token, None)


def trip_context(best_trips: list[dict], limit: int = 6) -> str:
    lines = [f"- {p['destination']} ({p['type']}): ${p['total']:,.0f} total for both travelers"
             for p in best_trips if p.get("total") is not None][:limit]
    return "Cheapest trips currently tracked:\n" + "\n".join(lines) if lines else "No complete trip prices tracked yet."


def _failure(status: int, detail: str) -> AssistantError:
    if status == 400:
        return AssistantError("The bridge rejected the request (invalid body or prompt length).")
    if status == 401:
        return AssistantError("The bridge rejected our token: check BRIDGE_TOKEN matches the bridge's token.", fatal=True)
    if status == 403:
        return AssistantError("The bridge does not allow this app to connect (client not in its allowed list).", fatal=True)
    if status == 429:
        return AssistantError("The bridge is busy with another request; try again in a minute.", 429)
    if status == 504:
        return AssistantError("Claude timed out on the bridge (about 3 minutes). Try a simpler question.", 504)
    if status == 502:
        return AssistantError(f"Claude failed on the bridge: {detail}".strip(": "))
    return AssistantError(f"Unexpected bridge response ({status}).")


def request(prompt: str, session_id: str | None = None, tools_url: str | None = None,
            tools_token: str | None = None, *, wait: float = 5.0) -> dict:
    """POST one prompt to the bridge. Returns {"answer", "session_id"}. Retries a busy (429) bridge with backoff.
    `wait` is how long to queue behind another request from this app before giving up as busy."""
    if not enabled():
        raise AssistantError("Claude is not configured: it needs a login (APP_PASSWORD_HASH) plus BRIDGE_URL and BRIDGE_TOKEN", 503, fatal=True)
    if not 1 <= len(prompt) <= MAX_PROMPT:
        raise AssistantError(f"The question is too long for the bridge (limit {MAX_PROMPT} characters).", 400)
    body = {"prompt": prompt, "session_id": session_id}
    if tools_url and tools_token:
        body.update(tools_url=tools_url, tools_token=tools_token)
    headers = {"Authorization": f"Bearer {bridge_token()}"}
    if not _bridge_lock.acquire(timeout=wait):
        raise AssistantError("Another Claude request from this app is still running; try again shortly.", 429)
    try:
        for attempt in range(len(BUSY_BACKOFF) + 1):
            try:
                r = httpx.post(bridge_url() + "/ask", headers=headers, timeout=TIMEOUT, json=body)
            except httpx.TimeoutException as e:
                raise AssistantError("Timed out waiting for the Claude bridge.", 504) from e
            except httpx.HTTPError as e:
                raise AssistantError(f"Cannot reach Claude bridge: {e.__class__.__name__}") from e
            if r.status_code == 429 and attempt < len(BUSY_BACKOFF):
                time.sleep(BUSY_BACKOFF[attempt])
                continue
            break
    finally:
        _bridge_lock.release()
    if r.status_code != 200:
        try:
            detail = str(r.json().get("error", ""))[:200]
        except (ValueError, AttributeError):
            detail = ""
        raise _failure(r.status_code, detail)
    try:
        reply = r.json()
        answer, session = reply.get("answer", ""), reply.get("session_id")
    except (ValueError, AttributeError):
        raise AssistantError("The bridge sent a reply we could not read.") from None
    if not isinstance(answer, str) or not (session is None or isinstance(session, str)):
        raise AssistantError("The bridge sent a reply we could not read.")
    return {"answer": answer, "session_id": session}


def ask(question: str, context: str, session_id: str | None = None) -> dict:
    """An interactive question. Attaches the app's own tools (with a one-question token) when APP_BASE_URL is set."""
    token = tools_url = None
    if tools_enabled():
        token, tools_url = mint_token(), app_base_url() + "/mcp"
    try:
        return request(f"{context}\n\nQuestion: {question}", session_id, tools_url, token)
    finally:
        if token:
            revoke(token)
