#!/bin/sh
# Generate runtime config from env vars (Railway / Docker friendly).
# Idempotent: only writes the file if it does not exist yet, so user edits
# in the mounted volume survive redeploys.

set -e

CONFIG_PATH="${OPENJARVIS_CONFIG:-/data/config.toml}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "[entrypoint] Writing initial config to $CONFIG_PATH"
  cat > "$CONFIG_PATH" <<EOF
# Auto-generated on first boot. Edit freely; will not be overwritten.

[server]
host = "0.0.0.0"

[engine]
default = "cloud"

[channel]
enabled = ${JARVIS_CHANNEL_ENABLED:-true}
default_channel = "${JARVIS_DEFAULT_CHANNEL:-telegram}"
default_agent = "${JARVIS_DEFAULT_AGENT:-simple}"

[channel.telegram]
bot_token = ""
allowed_chat_ids = "${TELEGRAM_ALLOWED_CHAT_IDS:-}"
parse_mode = "Markdown"
EOF
else
  echo "[entrypoint] Reusing existing config at $CONFIG_PATH"
fi

export OPENJARVIS_CONFIG="$CONFIG_PATH"

exec jarvis serve --host 0.0.0.0 --port "${PORT:-8000}"
