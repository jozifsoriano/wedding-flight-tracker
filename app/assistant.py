"""Client for the Claude Code bridge (see bridge/). Settings come from the environment, never config.yaml."""
import os

import httpx

TIMEOUT = 200.0   # the bridge allows claude 180s


class AssistantError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


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
    try:
        r = httpx.post(url, headers=headers, timeout=TIMEOUT,
                       json={"prompt": f"{context}\n\nQuestion: {question}", "session_id": session_id})
    except httpx.HTTPError as e:
        raise AssistantError(f"Cannot reach Claude bridge: {e.__class__.__name__}") from e
    if r.status_code != 200:
        try:
            detail = r.json().get("error", "")
        except ValueError:
            detail = ""
        raise AssistantError(f"Bridge error ({r.status_code}): {detail}".strip(), 429 if r.status_code == 429 else 502)
    body = r.json()
    return {"answer": body.get("answer", ""), "session_id": body.get("session_id")}
