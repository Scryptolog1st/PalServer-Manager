#!/usr/bin/env bash
set -euo pipefail

ENABLE_SSH=0
if [[ ${1:-} == "--enable-ssh" ]]; then ENABLE_SSH=1; fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo ./scripts/install-linux.sh [--enable-ssh]" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/palserver-manager
CONFIG_DIR=/etc/palserver-manager

install_prereqs() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip
    if [[ $ENABLE_SSH -eq 1 ]]; then apt-get install -y openssh-server; fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip
    if [[ $ENABLE_SSH -eq 1 ]]; then dnf install -y openssh-server; fi
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip
    if [[ $ENABLE_SSH -eq 1 ]]; then yum install -y openssh-server; fi
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm python python-pip
    if [[ $ENABLE_SSH -eq 1 ]]; then pacman -S --noconfirm openssh; fi
  else
    echo "No supported package manager found. Install Python 3.11+, pip and venv manually." >&2
    exit 1
  fi
}

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  install_prereqs
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python was not found after installing prerequisites." >&2
  exit 1
fi

# Some Debian-family systems have Python but not the venv module.
if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  install_prereqs
fi

if [[ $ENABLE_SSH -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd
  fi
fi

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install "${ROOT_DIR}[agent]"

export PALSERVER_MANAGER_CONFIG="$CONFIG_DIR/config.json"
"$INSTALL_DIR/venv/bin/python" - <<'PY'
from palserver_manager.config import load_config, save_config
cfg = load_config()
save_config(cfg)
print("Created configuration.")
print("Agent token:", cfg.agent.token)
PY
chmod 600 "$CONFIG_DIR/config.json"

if [[ -d /run/systemd/system ]]; then
  cp "$ROOT_DIR/scripts/palserver-manager-agent.service" /etc/systemd/system/palserver-manager-agent.service
  systemctl daemon-reload
  systemctl enable --now palserver-manager-agent
else
  echo "Warning: systemd was not detected. The agent was installed but not registered for automatic startup."
  echo "Start it manually with: PALSERVER_MANAGER_CONFIG=$CONFIG_DIR/config.json $INSTALL_DIR/venv/bin/palserver-agent"
fi

cat > /usr/local/bin/palserver-manager <<WRAPPER
#!/usr/bin/env bash
export PALSERVER_MANAGER_CONFIG="$CONFIG_DIR/config.json"
exec "$INSTALL_DIR/venv/bin/palserver-manager" "\$@"
WRAPPER
chmod +x /usr/local/bin/palserver-manager

cat > /usr/local/bin/palserver-agent <<WRAPPER
#!/usr/bin/env bash
export PALSERVER_MANAGER_CONFIG="$CONFIG_DIR/config.json"
exec "$INSTALL_DIR/venv/bin/palserver-agent" "\$@"
WRAPPER
chmod +x /usr/local/bin/palserver-agent

echo
echo "PalServer Manager installed."
echo "Config: $CONFIG_DIR/config.json"
echo "Agent: 127.0.0.1:8765 (loopback only)"
echo "Use SSH-tunnel mode from remote clients."
if [[ $ENABLE_SSH -eq 0 ]]; then echo "Tip: rerun with --enable-ssh if this host does not already run SSH."; fi
echo "Edit the config paths/passwords, then restart the agent service or process."
