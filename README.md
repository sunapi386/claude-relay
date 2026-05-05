# claude-relay

OpenAI-compatible API server that proxies requests through the authenticated Claude Code CLI. Use your Pro/Pro Max subscription as a local LLM API backend for tools like [OpenClaw](https://github.com/openclaw/openclaw), Open WebUI, or anything that speaks the OpenAI `/v1/chat/completions` protocol.

## Install & Run

```bash
curl -fsSL https://raw.githubusercontent.com/sunapi386/claude-relay/main/install.sh | bash
```

Or clone and run manually:

```bash
git clone https://github.com/sunapi386/claude-relay.git
cd claude-relay
./install.sh
```

The installer is idempotent — safe to run multiple times. It installs dependencies, sets up the systemd service, and starts the relay.

### Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` on PATH)
- Active Claude Pro/Pro Max subscription

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

## Usage

Once running (default port 5005):

```bash
# Health check
curl http://localhost:5005/v1/health

# Chat completion
curl http://localhost:5005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-20250918", "messages": [{"role": "user", "content": "Hello"}]}'

# Streaming
curl -N http://localhost:5005/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-20250918", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

### Use with OpenAI-compatible clients

Point any OpenAI SDK or tool at `http://localhost:5005/v1`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:5005/v1", api_key="none")
response = client.chat.completions.create(
    model="claude-opus-4-20250918",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completions (streaming + non-streaming) |
| `/v1/models` | GET | List available Claude models |
| `/v1/health` | GET | Health check + CLI version |

## Model ID mapping

The relay maps OpenAI-style model IDs to Claude CLI model IDs automatically:

| Request model ID | CLI model ID |
|---|---|
| `claude-opus-4-20250918` | `claude-opus-4-6` |
| `claude-sonnet-4-20250514` | `claude-sonnet-4-6` |

Any unrecognized model ID is passed through as-is to the CLI.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CLAUDE_BIN` | `claude` (from PATH) | Path to the Claude CLI binary |
| `PORT` | `5005` | Server port (also settable via `--port`) |
| `HOST` | `0.0.0.0` | Bind address (also settable via `--host`) |

## Managing the service

```bash
# Status
systemctl status claude-relay

# Restart
systemctl restart claude-relay

# Logs
journalctl -u claude-relay -f

# Stop
systemctl stop claude-relay

# Uninstall
systemctl stop claude-relay && systemctl disable claude-relay
rm /etc/systemd/system/claude-relay.service
rm -rf /opt/claude-relay /opt/relay-venv
systemctl daemon-reload
```

## Refreshing OAuth credentials

OAuth tokens expire periodically. When you see 401 errors:

```bash
# Re-copy from the machine where Claude CLI is authenticated
scp ~/.claude/.credentials.json root@<relay-host>:/root/.claude/.credentials.json
```

No service restart needed — the next CLI invocation picks up the new token.

## Production setup (Proxmox LXC)

For running in an isolated container, see the detailed guide below.

<details>
<summary>Proxmox LXC deployment guide</summary>

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

Clone from the base template:

```bash
# On Proxmox host
pct clone 304 306 --hostname claude-relay --full
pct start 306
```

### Step 2: Set up SSH access

```bash
pct exec 306 -- bash -c '
  mkdir -p /root/.ssh
  echo "ssh-ed25519 AAAA... user@host" >> /root/.ssh/authorized_keys
  chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
'
```

### Step 3: Install and run

```bash
ssh root@<relay-ip> 'curl -fsSL https://raw.githubusercontent.com/sunapi386/claude-relay/main/install.sh | bash'
```

### Step 4: Copy Claude CLI credentials

From the machine where you're logged into Claude Code:

```bash
scp ~/.claude/.credentials.json root@<relay-ip>:/root/.claude/.credentials.json
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

### Step 5: Firewall rules (Proxmox)

```bash
# On relay LXC (306): allow inbound from OpenClaw
pvesh create /nodes/pve/lxc/306/firewall/rules \
  --type in --action ACCEPT --dport 5005 --proto tcp \
  --source 192.168.2.31 --enable 1 \
  --comment "Allow OpenClaw to relay"

# On OpenClaw LXC (305): allow outbound to relay (before LAN DROP rule)
pvesh create /nodes/pve/lxc/305/firewall/rules \
  --type out --action ACCEPT --dest 192.168.2.33 --proto tcp \
  --dport 5005 --enable 1 \
  --comment "Allow relay on claude-relay LXC" \
  --pos 2
```

### Step 6: Configure OpenClaw

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

</details>

## Limitations

- Each request spawns a new `claude` CLI process (no connection pooling)
- No tool_use passthrough — the relay converts to text-only responses
- OAuth tokens expire periodically; re-copy credentials when you get 401s
- Single-tenant by design — one CLI auth, one user

## License

MIT
