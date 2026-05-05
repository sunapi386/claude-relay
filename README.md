# claude-relay

OpenAI-compatible API server that proxies requests through the authenticated Claude Code CLI. Use your Pro/Pro Max subscription as a local LLM API backend for tools like [OpenClaw](https://github.com/openclaw/openclaw), Open WebUI, or anything that speaks the OpenAI `/v1/chat/completions` protocol.

## How it works

```
Client (OpenClaw, curl, etc.)
  → POST /v1/chat/completions (OpenAI format)
    → claude-relay (Flask)
      → claude -p --output-format json (CLI subprocess)
        → Anthropic API (authenticated via your subscription)
      ← parsed JSON
    ← OpenAI-format response
```

Prompt is passed via stdin to the CLI to avoid shell injection and argument length limits.

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completions (streaming + non-streaming) |
| `/v1/models` | GET | List available Claude models |
| `/v1/health` | GET | Health check + CLI version |

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --port 5005
```

Test it:

```bash
# Health check
curl http://localhost:5005/v1/health

# Chat
curl http://localhost:5005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-20250918", "messages": [{"role": "user", "content": "Hello"}]}'

# Streaming
curl -N http://localhost:5005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-20250918", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

## Production setup (Proxmox LXC)

We run this in an isolated LXC container on Proxmox, separate from the consumer (OpenClaw). This keeps the Claude CLI credentials contained and firewalled.

### Architecture

```
MacBook (browser)
  → SSH tunnel (-L 18789:192.168.2.31:18789)
    → OpenClaw LXC (192.168.2.31:18789)  — CT 305, "openclaw-instance"
      → Claude Relay LXC (192.168.2.33:5005)  — CT 306, "claude-relay"
        → Claude CLI → Anthropic API
```

Both LXCs are cloned from a hardened Ubuntu 24.04 template (CT 304) with:
- Unprivileged container with nesting
- SSH key-only auth, root password disabled
- Postfix disabled, unnecessary SUID bits removed
- Unattended security upgrades

### Step 1: Create the LXC

Clone from the base template (assumes CT 304 exists — see OpenClaw setup):

```bash
# On Proxmox host
pct clone 304 306 --hostname claude-relay --full
pct start 306
```

### Step 2: Set up SSH access

```bash
# On Proxmox host
pct exec 306 -- bash -c '
  mkdir -p /root/.ssh
  echo "ssh-ed25519 AAAA... user@host" >> /root/.ssh/authorized_keys
  chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
'
```

### Step 3: Install Claude CLI + dependencies

```bash
ssh root@<relay-ip> '
  apt-get update -qq
  apt-get install -y -qq python3-pip python3-venv
  npm install -g @anthropic-ai/claude-code@latest
'
```

### Step 4: Copy Claude CLI credentials

From the machine where you're logged into Claude Code:

```bash
ssh root@<relay-ip> 'mkdir -p /root/.claude && chmod 700 /root/.claude'
scp ~/.claude/.credentials.json root@<relay-ip>:/root/.claude/.credentials.json
ssh root@<relay-ip> 'chmod 600 /root/.claude/.credentials.json'
```

Clear hooks from the copied settings (they reference scripts that don't exist on the relay):

```bash
ssh root@<relay-ip> 'python3 -c "
import json
with open(\"/root/.claude/settings.json\") as f:
    d = json.load(f)
d[\"hooks\"] = {}
with open(\"/root/.claude/settings.json\", \"w\") as f:
    json.dump(d, f, indent=2)
"'
```

Verify:

```bash
ssh root@<relay-ip> 'claude -p --output-format json "Say hi"'
```

> **Note:** OAuth tokens expire. If you get a 401, re-copy `.credentials.json` from the host machine. The CLI handles token refresh automatically if the refresh token is still valid.

### Step 5: Deploy the relay server

```bash
scp server.py root@<relay-ip>:/opt/claude-relay.py
ssh root@<relay-ip> '
  python3 -m venv /opt/relay-venv
  /opt/relay-venv/bin/pip install flask
'
```

### Step 6: Create systemd service

```bash
ssh root@<relay-ip> 'cat > /etc/systemd/system/claude-relay.service << EOF
[Unit]
Description=Claude CLI OpenAI-compatible API Relay
After=network.target

[Service]
Type=simple
Environment=HOME=/root
ExecStart=/opt/relay-venv/bin/python /opt/claude-relay.py --port 5005 --host 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable claude-relay
systemctl start claude-relay
'
```

### Step 7: Firewall rules (Proxmox)

On the Proxmox host, allow the OpenClaw LXC to reach the relay:

```bash
# On relay LXC (306): allow inbound from OpenClaw
pvesh create /nodes/pve/lxc/306/firewall/rules \
  --type in --action ACCEPT --dport 5005 --proto tcp \
  --source 192.168.2.31 --enable 1 \
  --comment "Allow OpenClaw to relay"

# On OpenClaw LXC (305): allow outbound to relay
# Insert BEFORE the LAN DROP rule
pvesh create /nodes/pve/lxc/305/firewall/rules \
  --type out --action ACCEPT --dest 192.168.2.33 --proto tcp \
  --dport 5005 --enable 1 \
  --comment "Allow relay on claude-relay LXC" \
  --pos 2
```

### Step 8: Configure OpenClaw to use the relay

In the OpenClaw LXC, update `~/.openclaw/openclaw.json`:

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "claude-relay": {
        "baseUrl": "http://192.168.2.33:5005/v1",
        "apiKey": "none",
        "api": "openai-completions",
        "models": [
          {
            "id": "claude-opus-4-20250918",
            "name": "Claude Opus 4 (via relay)",
            "reasoning": true,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 64000
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "claude-relay/claude-opus-4-20250918"
      }
    }
  }
}
```

Then restart the gateway:

```bash
systemctl --user restart openclaw-gateway
```

## Model ID mapping

The relay maps OpenAI-style model IDs to Claude CLI model IDs automatically:

| Request model ID | CLI model ID |
|---|---|
| `claude-opus-4-20250918` | `claude-opus-4-6` |
| `claude-sonnet-4-20250514` | `claude-sonnet-4-6` |

Any unrecognized model ID is passed through as-is.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_BIN` | `claude` (from PATH) | Path to the Claude CLI binary |

## Refreshing OAuth credentials

OAuth tokens expire periodically. When you see 401 errors, re-copy from the host machine:

```bash
scp ~/.claude/.credentials.json root@<relay-ip>:/root/.claude/.credentials.json
```

No service restart needed — the next CLI invocation picks up the new token.

## Limitations

- Each request spawns a new `claude` CLI process (no connection pooling)
- No tool_use passthrough — the relay converts to text-only responses
- OAuth tokens expire periodically; re-copy credentials when you get 401s
- Single-tenant by design — one CLI auth, one user
- Systemd service needs `Environment=HOME=/root` to find CLI config
