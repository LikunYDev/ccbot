#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Reinstall the package so the systemd service picks up code changes
echo "Installing ccbot from ${PROJECT_DIR}..."
uv tool install --force --reinstall "$PROJECT_DIR" 2>&1

# Restart the systemd user service
echo "Restarting ccbot service..."
systemctl --user restart ccbot

# Wait for startup and verify
sleep 2
if systemctl --user is-active --quiet ccbot; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    journalctl --user -u ccbot --since "5 sec ago" --no-pager | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot service failed to start. Logs:"
    echo "----------------------------------------"
    journalctl --user -u ccbot --since "10 sec ago" --no-pager | tail -30
    echo "----------------------------------------"
    exit 1
fi
