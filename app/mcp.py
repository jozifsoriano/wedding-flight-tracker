"""Minimal MCP server over streamable HTTP (JSON responses only, no SSE, no sessions).

Protocol only: main.py supplies the tools. Enough for Claude Code's `type: http` MCP client:
initialize, ping, tools/list, tools/call.
"""
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)
SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = ("Tools for the wedding flight tracker. Only destinations managed by the app can be changed "
                "(config.yaml ones until the user moves them into the app). Removing a destination needs the "
                "user's approval in the Ask Claude tab.")


class ToolError(Exception):
    """A failure to report to the model as a tool result (isError), not a protocol error."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], Any]
    read_only: bool = False


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call(tool: Tool, arguments: dict) -> dict:
    try:
        result = tool.handler(arguments or {})
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception:  # never leak internals to the model
        log.exception("MCP tool %s failed", tool.name)
        return {"content": [{"type": "text", "text": "Internal error while running the tool."}], "isError": True}


def handle(message: Any, tools: dict[str, Tool]) -> dict | None:
    """Return the JSON-RPC response, or None for notifications (reply 202)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    method, msg_id, params = message["method"], message.get("id"), message.get("params") or {}
    if msg_id is None:   # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "wedding-flight-tracker", "version": "1.0"},
            "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.schema,
             "annotations": {"readOnlyHint": t.read_only, "destructiveHint": False}}
            for t in tools.values()]}}
    if method == "tools/call":
        tool = tools.get(params.get("name"))
        if tool is None:
            return _error(msg_id, -32602, f"Unknown tool: {params.get('name')}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": _call(tool, params.get("arguments"))}
    return _error(msg_id, -32601, f"Method not found: {method}")
