#!/usr/bin/env bash
#
# Deploy LXC with pve2netbox from a Proxmox VE host.
# Run on a PVE node (as root):
#   ./deploy-from-pve.sh
#   CTID=201 BRIDGE=vmbr1 ./deploy-from-pve.sh
#   ./deploy-from-pve.sh --no-install   # only create container, do not install app
#
# In interactive mode (run from terminal) it prompts for: container ID, name, storage.
# Variables (optional): CTID, STORAGE, BRIDGE, HOSTNAME, ROOTFS_SIZE, MEMORY, TEMPLATE_STORAGE
#
# If .env exists next to the script or in repo root, it is copied into the container as /etc/pve2netbox/env.
#
set -e

DEPLOY_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ "$(id -u)" -eq 0 ]] || { echo "Run this script on the Proxmox host as root."; exit 1; }

command -v pct &>/dev/null || { echo "Command pct not found. Run this script on a Proxmox VE node."; exit 1; }

# Parameters (can be set via environment variables)
CTID_FROM_ENV="${CTID:-}"
CTID="${CTID:-200}"
STORAGE="${STORAGE:-}"           # empty = auto (local-lvm or local)
BRIDGE="${BRIDGE:-vmbr0}"
PVE_NODE_HOSTNAME="$(hostname 2>/dev/null || true)"
PVE_NODE_FQDN="$(hostname -f 2>/dev/null || true)"
HOSTNAME_INPUT="${LXC_HOSTNAME:-${HOSTNAME:-}}"
if [[ -z "$HOSTNAME_INPUT" ]] || [[ "$HOSTNAME_INPUT" == "$PVE_NODE_HOSTNAME" ]] || [[ -n "$PVE_NODE_FQDN" && "$HOSTNAME_INPUT" == "$PVE_NODE_FQDN" ]]; then
  HOSTNAME="lxc-pve2netbox"
else
  HOSTNAME="$HOSTNAME_INPUT"
fi
ROOTFS_SIZE="${ROOTFS_SIZE:-2}"  # GiB
MEMORY="${MEMORY:-256}"          # MiB
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
NO_INSTALL="${NO_INSTALL:-}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/aburdey08/pve2netbox/master}"
REPO_GIT="${REPO_GIT:-https://github.com/aburdey08/pve2netbox.git}"

for arg in "$@"; do
  case "$arg" in
    --no-install) NO_INSTALL=1 ;;
    -h|--help)
      echo "Usage: $0 [--no-install]"
      echo "Variables: CTID, STORAGE, BRIDGE, LXC_HOSTNAME (or HOSTNAME), ROOTFS_SIZE, MEMORY, TEMPLATE_STORAGE, REPO_RAW, REPO_GIT"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: $0 [--no-install]"
      exit 1
      ;;
  esac
done

# Next free CTID
next_ctid() {
  local id=200
  while pct status "$id" &>/dev/null; do
    ((id++))
  done
  echo "$id"
}

# --- Interactive input (when not set by variables) ---
NEXT_FREE=$(next_ctid)

if [[ -t 0 ]]; then
  echo "=== LXC container parameters ==="
  default_ctid="${CTID_FROM_ENV:-$NEXT_FREE}"
  read -p "Container ID (100–999999999) [${default_ctid}]: " input_ctid
  CTID="${input_ctid:-$default_ctid}"

  read -p "Container name (hostname) [${HOSTNAME}]: " input_hostname
  HOSTNAME="${input_hostname:-$HOSTNAME}"

  echo ""
  echo "Available storages for container rootfs:"
  storages=()
  while read -r name _; do
    [[ -z "$name" ]] && continue
    case "$name" in Name|Storage|Nazwa) continue ;; esac
    storages+=("$name")
  done < <(pvesm status 2>/dev/null)
  if [[ ${#storages[@]} -eq 0 ]]; then
    echo "  (pvesm status returned no list, using local)"
    storages=(local local-lvm)
  fi
  default_storage=""
  for s in local-lvm local; do
    if [[ " ${storages[*]} " == *" $s "* ]]; then
      default_storage="$s"
      break
    fi
  done
  [[ -z "$default_storage" ]] && default_storage="${storages[0]:-local}"
  for i in "${!storages[@]}"; do
    echo "  $((i+1))) ${storages[$i]}"
  done
  read -p "Select storage (number or name) [${default_storage}]: " input_storage
  if [[ -n "$input_storage" ]]; then
    if [[ "$input_storage" =~ ^[0-9]+$ ]] && [[ "$input_storage" -ge 1 ]] && [[ "$input_storage" -le ${#storages[@]} ]]; then
      STORAGE="${storages[$((input_storage-1))]}"
    else
      STORAGE="$input_storage"
    fi
  else
    STORAGE="$default_storage"
  fi
  echo "[*] Selected storage: $STORAGE"
  echo ""
fi

CTID=$((CTID))

# Validate CTID
if pct status "$CTID" &>/dev/null; then
  echo "Container with ID $CTID already exists. Use another CTID (e.g. $(next_ctid)) or set CTID variable."
  exit 1
fi

# Storage: if not set (non-interactive) — auto
if [[ -z "$STORAGE" ]]; then
  if pvesm list local-lvm &>/dev/null; then
    STORAGE=local-lvm
  else
    STORAGE=local
  fi
  echo "[*] Container storage: $STORAGE"
fi

# Debian 12 template (path for pct create: storage:vztmpl/filename)
TEMPLATE_NAME="debian-12-standard"
TEMPLATE_PATH=""
while read -r path _; do
  if [[ "$path" == *"${TEMPLATE_NAME}"* ]] && [[ "$path" == *.tar.zst ]]; then
    TEMPLATE_PATH="$path"
    break
  fi
done < <(pveam list "$TEMPLATE_STORAGE" 2>/dev/null || true)

if [[ -z "$TEMPLATE_PATH" ]]; then
  echo "[*] Template ${TEMPLATE_NAME} not found in $TEMPLATE_STORAGE. Downloading..."
  pveam update
  LINE=$(pveam available 2>/dev/null | grep -E "${TEMPLATE_NAME}_.*\.tar\.zst" | head -1)
  AVAILABLE=$(echo "$LINE" | grep -oE "${TEMPLATE_NAME}_[0-9.-]+_amd64\.tar\.zst" | head -1)
  if [[ -z "$AVAILABLE" ]]; then
    echo "Could not find template ${TEMPLATE_NAME} in pveam available. Run: pveam available"
    exit 1
  fi
  pveam download "$TEMPLATE_STORAGE" "$AVAILABLE"
  TEMPLATE_PATH="${TEMPLATE_STORAGE}:vztmpl/${AVAILABLE}"
fi

echo "[*] Template: $TEMPLATE_PATH"

# Create container
echo "[*] Creating LXC $CTID ($HOSTNAME)..."
pct create "$CTID" "$TEMPLATE_PATH" \
  --rootfs "${STORAGE}:${ROOTFS_SIZE}" \
  --memory "$MEMORY" \
  --cores 1 \
  --hostname "$HOSTNAME" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --description "pve2netbox" \
  --onboot 1 \
  --start

echo "[*] Waiting for container to boot (15s)..."
sleep 15

# Install pve2netbox inside container
if [[ -n "$NO_INSTALL" ]]; then
  echo "[*] --no-install mode: app installation skipped."
  echo "    Manual install (ensure curl in container first: apt-get install -y curl):"
  echo "    pct exec $CTID -- env REPO_GIT=\"$REPO_GIT\" bash -c 'curl -sL $REPO_RAW/contrib/lxc/install.sh | bash'"
  exit 0
fi

echo "[*] Installing pve2netbox into container $CTID..."
echo "[*] Ensuring curl (and ca-certificates) in container..."
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates"
pct exec "$CTID" -- env REPO_RAW="$REPO_RAW" REPO_GIT="$REPO_GIT" bash -c "curl -sL $REPO_RAW/contrib/lxc/install.sh | bash"

ENV_COPIED=""
ENV_FILE=""
[[ -f "$DEPLOY_SCRIPT_DIR/.env" ]] && ENV_FILE="$DEPLOY_SCRIPT_DIR/.env"
[[ -z "$ENV_FILE" && -f "$DEPLOY_SCRIPT_DIR/../.env" ]] && ENV_FILE="$DEPLOY_SCRIPT_DIR/../.env"
[[ -z "$ENV_FILE" && -f "$DEPLOY_SCRIPT_DIR/../../.env" ]] && ENV_FILE="$DEPLOY_SCRIPT_DIR/../../.env"
if [[ -n "$ENV_FILE" ]]; then
  echo "[*] Copying .env into container (as /etc/pve2netbox/env)..."
  pct push "$CTID" "$ENV_FILE" /etc/pve2netbox/env
  pct exec "$CTID" -- chmod 600 /etc/pve2netbox/env
  ENV_COPIED=1
fi

echo ""
echo "[OK] LXC $CTID deployed and ready."
if [[ -n "$ENV_COPIED" ]]; then
  echo "  Config taken from .env (next to script). Edit if needed: pct exec $CTID -- nano /etc/pve2netbox/env"
else
  echo "  1. Enter container:  pct enter $CTID"
  echo "  2. Edit config: nano /etc/pve2netbox/env  (PVE_API_*, NB_API_*, intervals)"
fi
echo "  Enable service:  systemctl enable --now pve2netbox  (from container) or:"
echo "    pct exec $CTID -- systemctl enable --now pve2netbox"
echo ""
