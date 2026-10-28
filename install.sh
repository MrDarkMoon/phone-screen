#!/bin/sh
# Installs Phone Screen's background service (daemon.py) as a systemd user service, started
# with the session: it waits for the phone app's Start tracking. ./install.sh remove takes it out.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
UNIT=$HOME/.config/systemd/user/phone-screen.service
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
if [ "$1" = remove ]; then
    systemctl --user disable --now phone-screen.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "removed"
    exit 0
fi
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Phone Screen: track the phone and show its screen in VR while its app is tracking

[Service]
WorkingDirectory=$HERE
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$HERE/venv/bin/python -u $HERE/daemon.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now phone-screen.service
systemctl --user --no-pager status phone-screen.service | head -5
