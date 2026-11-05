# Claude bridge (runs on the homelab VM with Claude Code)

The tracker app calls this small service; it runs `claude -p` headless with the Slicktrip MCP attached.
OAuth for Slicktrip happens once on the VM, so no Slicktrip credential ever lives in the app or this repo.

## 1. Add Slicktrip and log in (once, on the VM, as the user that will run the bridge)

```bash
claude mcp add --transport http --scope user slicktrip https://slicktrip.com/mcp
claude            # then run /mcp, pick slicktrip and complete the OAuth login in a browser
claude mcp list   # slicktrip should show as connected
```

On a headless VM, open the login URL on another machine; if the redirect fails, paste the callback URL back into the prompt.

## 2. Install the bridge

```bash
sudo mkdir -p /opt/claude-bridge /var/lib/claude-bridge
sudo cp claude_bridge.py /opt/claude-bridge/
echo "BRIDGE_TOKEN=$(openssl rand -hex 32)" | sudo tee /etc/claude-bridge.env >/dev/null
sudo chmod 600 /etc/claude-bridge.env
# edit User= in claude-bridge.service, then:
sudo cp claude-bridge.service /etc/systemd/system/ && sudo systemctl enable --now claude-bridge
curl localhost:8787/health
```

Copy the same token into the tracker's `.env` as `CLAUDE_BRIDGE_TOKEN`, and set `CLAUDE_BRIDGE_URL=http://<claude-vm-ip>:8787`.

Notes: only `mcp__slicktrip` tools are pre-approved, so headless Claude cannot run shell commands or edit files. Keep port 8787 LAN-only. Optional env: `BRIDGE_PORT`, `BRIDGE_HOST`, `BRIDGE_TIMEOUT`, `BRIDGE_MAX_CONCURRENT`, `CLAUDE_BIN`, `MCP_SERVER_NAME`.
