#!/usr/bin/env bash
# Install pve2netbox inside LXC (Debian/Ubuntu).
# Run: from repo root — ./contrib/lxc/install.sh
# Or without repo: curl .../install.sh | sudo bash (install from git repo)

set -e

[[ "$(id -u)" -eq 0 ]] || { echo "Run this script as root (sudo)."; exit 1; }

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/aburdey08/pve2netbox/master}"
REPO_GIT="${REPO_GIT:-https://github.com/aburdey08/pve2netbox.git}"
INSTALL_DIR=/etc/pve2netbox
SYSTEMD_DIR=/etc/systemd/system

# Detect if script is run from repo (root = dir with .env.example and pyproject.toml)
SCRIPT_DIR=
if [[ -f ".env.example" && -f "pyproject.toml" && -d "contrib/systemd" ]]; then
  SCRIPT_DIR="$(pwd)"
fi

echo "[*] pve2netbox: installing in LXC..."

# Dependencies
if command -v apt-get &>/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3-pip git
elif command -v dnf &>/dev/null; then
  dnf install -y python3-pip git
else
  echo "Only apt and dnf are supported. Install python3-pip and git manually."
  exit 1
fi

# Install package
if [[ -n "$SCRIPT_DIR" ]]; then
  echo "[*] Installing from source (repo: $SCRIPT_DIR)"
  pip3 install --break-system-packages "$SCRIPT_DIR" 2>/dev/null || pip3 install "$SCRIPT_DIR"
else
  echo "[*] Installing from repo: $REPO_GIT"
  clone_dir=$(mktemp -d)
  git clone --depth 1 "$REPO_GIT" "$clone_dir"
  pip3 install --break-system-packages "$clone_dir" 2>/dev/null || pip3 install "$clone_dir"
  rm -rf "$clone_dir"
fi

# Config directory
mkdir -p "$INSTALL_DIR"

# Environment file
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/.env.example" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/env"
  echo "[*] Copied $INSTALL_DIR/env from .env.example — edit and fill in secrets."
else
  curl -sL "$REPO_RAW/.env.example" -o "$INSTALL_DIR/env"
  echo "[*] Downloaded $INSTALL_DIR/env — edit and fill in secrets."
fi
chmod 600 "$INSTALL_DIR/env"

# Systemd units
download_unit() {
  local name="$1"
  if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/contrib/systemd/$name" ]]; then
    cp "$SCRIPT_DIR/contrib/systemd/$name" "$SYSTEMD_DIR/$name"
  else
    curl -sL "$REPO_RAW/contrib/systemd/$name" -o "$SYSTEMD_DIR/$name"
  fi
}

download_unit "pve2netbox.service"
download_unit "pve2netbox-oneshot.service"
download_unit "pve2netbox.timer"
systemctl daemon-reload

echo ""
echo "[OK] Installation complete."
echo "  1. Edit $INSTALL_DIR/env (PVE_API_*, NB_API_*, and intervals if needed)."
echo "  2. Enable the service:"
echo "     systemctl enable --now pve2netbox     # long-running (recommended)"
echo "     or to run on a 5-minute timer:"
echo "     systemctl enable --now pve2netbox.timer"
echo ""
