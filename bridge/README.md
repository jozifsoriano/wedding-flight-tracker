# Claude bridge (runs on the homelab `claude` VM)

The tracker app calls this small service; it runs `claude -p` headless with two MCP servers attached:

| MCP server | Where | Auth | Used for |
|---|---|---|---|
| `slicktrip` | `https://slicktrip.com/mcp` | OAuth, done once on this VM (user scope) | flight/trip research |
| `wedding` | the tracker app, `APP_URL/mcp` | per-question token the app mints and revokes | list/add/update destinations, refresh prices, request removals |

Claude gets **no built-in tools** (no shell, no files). Removals are never executed by Claude: it queues them and you
click Approve in the app's **Ask Claude** tab.

## 1. Add Slicktrip and log in (once, as the user that will run the bridge)

```bash
claude mcp add --transport http --scope user slicktrip https://slicktrip.com/mcp
claude            # run /mcp, pick slicktrip, finish the OAuth login (open the URL on another machine if headless)
claude mcp list   # slicktrip should show as connected
```

## 2. Install the bridge

```bash
sudo mkdir -p /opt/claude-bridge && sudo cp claude_bridge.py /opt/claude-bridge/
sudo tee /etc/claude-bridge.env >/dev/null <<ENV
BRIDGE_TOKEN=$(openssl rand -hex 32)
BRIDGE_ALLOWED_APP_URL=http://<app-ip>:8000
BRIDGE_ALLOWED_CLIENTS=<app-ip>
ENV
sudo chmod 600 /etc/claude-bridge.env
# edit User= in claude-bridge.service, then:
sudo cp claude-bridge.service /etc/systemd/system/ && sudo systemctl enable --now claude-bridge
curl localhost:8791/health
```

Then in the tracker's `.env`: `CLAUDE_BRIDGE_URL=http://<claude-vm-ip>:8791`, `CLAUDE_BRIDGE_TOKEN=<same token>`,
`APP_URL=http://<app-ip>:8000` (omit `APP_URL` for a chat-only, read-only setup), then recreate the container.

## Environment

`BRIDGE_TOKEN` (required) · `BRIDGE_ALLOWED_APP_URL` (the bridge attaches the app's tools only to this URL) ·
`BRIDGE_ALLOWED_CLIENTS` (comma list of caller IPs; empty = any) · `BRIDGE_PORT` (8791) · `BRIDGE_HOST` ·
`BRIDGE_TIMEOUT` (180 s) · `BRIDGE_MAX_CONCURRENT` (1) · `CLAUDE_BIN` · `MCP_SERVER_NAME` (slicktrip)
