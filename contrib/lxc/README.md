# LXC: pve2netbox (including Proxmox VE)

Deploy in an LXC container. Operation modes are the same as in the [main README](../../README.md#operation-modes-shared-by-docker-lxc-systemd). Inside the container, systemd is used ([contrib/systemd/](../systemd/)).

## One-command deploy from PVE host — **Recommended!**

The **deploy-from-pve.sh** script creates an LXC (Debian 12), starts it, and installs pve2netbox. Run **on a Proxmox node as root**.

```bash
# Interactively prompts for: container ID, name, storage
./deploy-from-pve.sh
```

Or without cloning the repo:

```bash
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/deploy-from-pve.sh -o deploy-from-pve.sh
chmod +x deploy-from-pve.sh
./deploy-from-pve.sh
```

**Config from .env:** put `.env` in the same directory as `deploy-from-pve.sh`). It will be copied into the container as `/etc/pve2netbox/env` — no need to fill config manually. If not found there, the script also looks in `contrib/` and the repo root.

**Parameters** (environment variables): `CTID`, `STORAGE`, `BRIDGE`, `HOSTNAME`, `ROOTFS_SIZE`, `MEMORY`, `TEMPLATE_STORAGE`. Defaults: CTID — first free, HOSTNAME — `lxc-pve2netbox`. Examples: `CTID=201 BRIDGE=vmbr1 ./deploy-from-pve.sh`, `./deploy-from-pve.sh --no-install` (create LXC only).

After deploy: edit `/etc/pve2netbox/env` if needed, then `systemctl enable --now pve2netbox` (from inside the container or `pct exec <CTID> -- systemctl enable --now pve2netbox`).

---

## Configuration

With systemd in LXC: file **`/etc/pve2netbox/env`** (format `KEY=value`, no `export`). Mode samples: **env.single-run**, **env.simple-mode**, **env.combined-mode** in this directory — copy into the container as `/etc/pve2netbox/env` or use as `.env` before deploy-from-pve.sh.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| **PVE_API_HOST** | yes | DNS or IP of Proxmox VE host |
| **PVE_API_USER** | yes | PVE user (e.g. `netsync@pve`) |
| **PVE_API_TOKEN** | yes | PVE API token name |
| **PVE_API_SECRET** | yes | PVE API token secret |
| **NB_API_URL** | yes | NetBox URL (e.g. `https://netbox.example.org`) |
| **NB_API_TOKEN** | yes | NetBox API token |
| **PVE_API_VERIFY_SSL** | no | PVE SSL verification (`true`/`false`, default `true`) |
| **NB_CLUSTER_ID** | no | NetBox cluster ID |
| **NB_API_DELAY_SECONDS** | no | Delay between NetBox requests, sec (default `0.2`) |
| **NB_API_RETRY_TOTAL** | no | Retries on 502/503/429 (default `5`) |
| **NB_API_RETRY_BACKOFF** | no | Backoff factor between retries (default `1.0`) |
| **SYNC_INTERVAL_SECONDS** | no | Full sync interval, sec (modes 2 and 3) |
| **QUICK_CHECK_INTERVAL_SECONDS** | no | Quick check interval, sec (mode 3, e.g. `60`) |
| **SYNC_VMS** | no | Sync QEMU VMs (`true`/`false`, default `true`) |
| **SYNC_LXC** | no | Sync LXC (`true`/`false`, default `true`) |
| **VM_ROLE** | no | NetBox device role name or ID for VMs (e.g. `Virtual Machine`) |
| **LXC_ROLE** | no | NetBox device role name or ID for LXC (e.g. `Container`) |
| **DRY_RUN** | no | Check only, no changes (`true`/`false`) |
| **ENABLE_CLEANUP** | no | Remove from NetBox VMs missing in PVE (`true`/`false`) |
| **LOG_LEVEL** | no | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| **ENABLE_METRICS** | no | Enable Prometheus metrics (`true`/`false`) |
| **METRICS_PORT** | no | Metrics port (default `9090`) |

Full sample: [.env.example](../../.env.example).


## Update

From inside the container, run the install script (it will not overwrite existing `/etc/pve2netbox/env`), then restart the service:

```bash
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash
systemctl restart pve2netbox
```

## Install into an existing LXC

Enter the container (`pct enter <CTID>`), then:

**Option A** — one command (install from git repo + unit files and env from GitHub):

```bash
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash
```

**Option B** — from repo:

```bash
apt update && apt install -y git
git clone https://github.com/aburdey08/pve2netbox.git && cd pve2netbox
sudo ./contrib/lxc/install.sh
```

## Operation modes

Same three modes (single-run, simple, combined). Variables in `/etc/pve2netbox/env`.

## Run

- **Mode 1:** run `pve2netbox` manually or via cron.
- **Mode 2 or 3:** [contrib/systemd/](../systemd/) — `systemctl enable --now pve2netbox`.

## Check

```bash
systemctl status pve2netbox
journalctl -u pve2netbox -f
```

## Network

From LXC, PVE host/port (`PVE_API_HOST`) and NetBox URL (`NB_API_URL`) must be reachable.

---

**Summary:** container already exists — install from inside (replace CTID):

```bash
pct exec 200 -- bash -c 'curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash'
```

Then: edit `/etc/pve2netbox/env`, `systemctl enable --now pve2netbox`.
