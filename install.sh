#!/usr/bin/env bash
set -euo pipefail

# claude-relay installer
# Idempotent — safe to run multiple times.
# Installs dependencies, deploys server, creates systemd service.

INSTALL_DIR="/opt/claude-relay"
VENV_DIR="/opt/relay-venv"
SERVICE_NAME="claude-relay"
PORT="${PORT:-5005}"
HOST="${HOST:-0.0.0.0}"

info()  { echo "[claude-relay] $*"; }
error() { echo "[claude-relay] ERROR: $*" >&2; exit 1; }

# --- Preflight checks ---

command -v python3 >/dev/null 2>&1 || error "python3 not found. Install Python 3.10+."
command -v claude >/dev/null 2>&1  || error "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"

# Check Claude CLI is authenticated
if ! claude -p --output-format json "ping" 2>/dev/null | grep -q '"is_error":false'; then
    echo ""
    info "WARNING: Claude CLI may not be authenticated."
    info "Run 'claude' interactively to log in, then re-run this script."
    echo ""
fi

# --- Install dependencies ---

info "Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip flask

# --- Deploy server ---

info "Deploying server to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# Copy server.py from the same directory as this script, or download it
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/server.py" ]; then
    cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"
else
    info "Downloading server.py from GitHub..."
    curl -fsSL "https://raw.githubusercontent.com/sunapi386/claude-relay/main/server.py" \
        -o "$INSTALL_DIR/server.py"
fi

# --- Create systemd service ---

CLAUDE_BIN="$(command -v claude)"

info "Configuring systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Claude CLI OpenAI-compatible API Relay
After=network.target

[Service]
Type=simple
Environment=HOME=/root
Environment=CLAUDE_BIN=${CLAUDE_BIN}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/server.py --port ${PORT} --host ${HOST}
Restart=on-failure
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet

# --- Start or restart ---

if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Restarting service..."
    systemctl restart "$SERVICE_NAME"
else
    info "Starting service..."
    systemctl start "$SERVICE_NAME"
fi

# --- Verify ---

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Service is running on ${HOST}:${PORT}"
    info "Health check: curl http://localhost:${PORT}/v1/health"
    info ""
    info "Test it:"
    info "  curl http://localhost:${PORT}/v1/chat/completions \\"
    info "    -H 'Content-Type: application/json' \\"
    info "    -d '{\"model\": \"claude-opus-4-20250918\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}'"
else
    error "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 20"
fi
