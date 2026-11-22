#!/usr/bin/env bash
# Install Prometheus node_exporter from the official GitHub release.
#
#   sudo bash scripts/install_node_exporter.sh            # latest release
#   sudo bash scripts/install_node_exporter.sh v1.9.1     # pinned release
#
# The tarball is verified against the release's sha256sums.txt before install.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE="${1:-latest}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l) ARCH=armv7 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [[ "$RELEASE" == "latest" ]]; then
  RELEASE="$(curl -fsSL https://api.github.com/repos/prometheus/node_exporter/releases/latest \
    | grep -m1 '"tag_name"' | cut -d'"' -f4)"
fi
VERSION="${RELEASE#v}"
NAME="node_exporter-${VERSION}.linux-${ARCH}"
BASE="https://github.com/prometheus/node_exporter/releases/download/${RELEASE}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading ${NAME} (${RELEASE})..."
curl -fsSL --retry 3 -o "$TMP/${NAME}.tar.gz" "${BASE}/${NAME}.tar.gz"
curl -fsSL --retry 3 -o "$TMP/sha256sums.txt" "${BASE}/sha256sums.txt"

echo "Verifying checksum..."
(cd "$TMP" && grep "  ${NAME}.tar.gz$" sha256sums.txt | sha256sum -c -)

tar -xzf "$TMP/${NAME}.tar.gz" -C "$TMP"
install -m 0755 "$TMP/${NAME}/node_exporter" /usr/local/bin/node_exporter
install -m 0644 "$REPO_DIR/deploy/node-exporter.service" /etc/systemd/system/node-exporter.service

systemctl daemon-reload
systemctl enable --now node-exporter
sleep 1
systemctl --no-pager --lines=0 status node-exporter || true

echo
echo "node_exporter $(/usr/local/bin/node_exporter --version 2>&1 | head -1)"
curl -fsS http://127.0.0.1:9100/metrics | head -3
echo "OK: metrics available on http://127.0.0.1:9100/metrics"
