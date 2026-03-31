#!/usr/bin/env bash
# =============================================================================
# Battery Monitor — Installation Script
# Installs to /opt/battery-monitor, sets up a systemd USER service,
# and creates launcher scripts in ~/.local/bin/.
# Run as a regular user (no sudo needed for most steps; sudo only for /opt).
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
die()     { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── Paths ─────────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/battery-monitor"
SERVICE_DIR="${HOME}/.config/systemd/user"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/battery-monitor"
DATA_DIR="${HOME}/.local/share/battery-monitor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${BOLD}========================================${RESET}"
echo -e "${BOLD}  Battery Monitor — Installer${RESET}"
echo -e "${BOLD}========================================${RESET}"
echo

# ── 1. System packages ────────────────────────────────────────────────────────
info "Installing system packages (requires sudo)…"
sudo apt-get update -qq
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-3.0 \
    gir1.2-notify-0.7 \
    upower \
    powertop \
    libdbus-1-dev \
    libdbus-glib-1-dev \
    python3-dbus \
    libcairo2-dev \
    libgirepository1.0-dev \
    pkg-config || warn "Some packages failed — continuing"
success "System packages done."

# ── 2. Python packages ────────────────────────────────────────────────────────
info "Installing Python packages…"
pip3 install --user -r "${PROJECT_DIR}/requirements.txt" --quiet
success "Python packages done."

# ── 3. Copy application to /opt ───────────────────────────────────────────────
info "Copying application to ${INSTALL_DIR}…"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp -r "${PROJECT_DIR}/src"    "${INSTALL_DIR}/"
sudo cp -r "${PROJECT_DIR}/config" "${INSTALL_DIR}/"
sudo chmod -R 755 "${INSTALL_DIR}"
success "Application files installed."

# ── 4. User directories ───────────────────────────────────────────────────────
info "Creating user data directories…"
mkdir -p "${CONFIG_DIR}" "${DATA_DIR}/logs" "${BIN_DIR}"
success "Directories ready."

# ── 5. User config (only if not already present) ──────────────────────────────
if [[ ! -f "${CONFIG_DIR}/battery-monitor.conf" ]]; then
    cp "${PROJECT_DIR}/config/battery-monitor.conf" "${CONFIG_DIR}/battery-monitor.conf"
    success "Default config written to ${CONFIG_DIR}/battery-monitor.conf"
else
    info "User config already exists — not overwritten."
fi

# ── 6. Launcher scripts ───────────────────────────────────────────────────────
info "Creating launcher scripts in ${BIN_DIR}…"

cat > "${BIN_DIR}/battery-monitor-daemon" <<'EOF'
#!/usr/bin/env bash
exec python3 /opt/battery-monitor/src/daemon/battery_daemon.py "$@"
EOF

cat > "${BIN_DIR}/battery-monitor-ui" <<'EOF'
#!/usr/bin/env bash
exec python3 /opt/battery-monitor/src/ui/battery_widget.py "$@"
EOF

chmod +x "${BIN_DIR}/battery-monitor-daemon" "${BIN_DIR}/battery-monitor-ui"
success "Launchers created."

# ── 7. Desktop entry ──────────────────────────────────────────────────────────
APPS_DIR="${HOME}/.local/share/applications"
mkdir -p "${APPS_DIR}"
cat > "${APPS_DIR}/battery-monitor.desktop" <<EOF
[Desktop Entry]
Name=Battery Monitor
Comment=Monitor battery usage and top power-consuming processes
Exec=${BIN_DIR}/battery-monitor-ui
Icon=battery
Terminal=false
Type=Application
Categories=System;Monitor;
Keywords=battery;power;monitor;
StartupNotify=false
EOF
success "Desktop entry created."

# ── 8. systemd user service ───────────────────────────────────────────────────
info "Installing systemd user service…"
mkdir -p "${SERVICE_DIR}"

cat > "${SERVICE_DIR}/battery-monitor.service" <<EOF
[Unit]
Description=Battery Monitor Daemon
Documentation=file:///opt/battery-monitor
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/battery-monitor/src/daemon/battery_daemon.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
CPUQuota=5%%
MemoryMax=128M
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable battery-monitor.service
systemctl --user start  battery-monitor.service
success "Systemd user service enabled and started."

# ── 9. Verify ─────────────────────────────────────────────────────────────────
echo
info "Checking service status…"
sleep 2
if systemctl --user is-active --quiet battery-monitor.service; then
    success "Daemon is running."
else
    warn "Daemon may not be running yet. Check with:"
    warn "  systemctl --user status battery-monitor"
    warn "  journalctl --user -u battery-monitor -n 40"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo
echo -e "  ${BOLD}Start UI widget:${RESET}   battery-monitor-ui"
echo -e "  ${BOLD}Daemon status:${RESET}     systemctl --user status battery-monitor"
echo -e "  ${BOLD}Daemon logs:${RESET}       journalctl --user -u battery-monitor -f"
echo -e "  ${BOLD}Stop daemon:${RESET}       systemctl --user stop battery-monitor"
echo -e "  ${BOLD}Disable autostart:${RESET} systemctl --user disable battery-monitor"
echo -e "  ${BOLD}Config file:${RESET}       ${CONFIG_DIR}/battery-monitor.conf"
echo -e "  ${BOLD}Data / DB:${RESET}         ${DATA_DIR}/"
echo
warn "If ${BIN_DIR} is not in your PATH, add this to ~/.bashrc or ~/.zshrc:"
warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
