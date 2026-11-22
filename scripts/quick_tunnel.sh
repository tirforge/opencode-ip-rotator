#!/usr/bin/env bash
# Cloudflare quick tunnel for the OpenCode proxy dashboard.
#
# Starts `cloudflared tunnel --url ...` and republishes the rotating
# trycloudflare.com hostname to a file the dashboard reads via /api/tunnel.
# Runs forever; systemd restarts it if cloudflared dies.
set -u

TARGET="${TUNNEL_TARGET:-http://127.0.0.1:8000}"
URL_FILE="${TUNNEL_URL_FILE:-$(dirname "$0")/../data/tunnel_url}"
mkdir -p "$(dirname "$URL_FILE")"

log() { printf '[quick-tunnel] %s\n' "$*"; }

if ! command -v cloudflared >/dev/null 2>&1; then
    log "cloudflared is not installed"
    exit 1
fi

while true; do
    rm -f "$URL_FILE"
    log "opening quick tunnel to $TARGET"
    cloudflared tunnel --url "$TARGET" --no-autoupdate 2>&1 | while IFS= read -r line; do
        url="$(printf '%s\n' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -n 1)"
        if [ -n "$url" ]; then
            printf '%s\n' "$url" > "$URL_FILE"
            log "published $url"
        fi
    done
    log "cloudflared exited; retrying in 10s"
    rm -f "$URL_FILE"
    sleep 10
done
