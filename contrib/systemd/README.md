# Systemd: pve2netbox

Unit files for running on host or in LXC (without Docker). Operation modes are the same as in the [main README](../../README.md#operation-modes-shared-by-docker-lxc-systemd).

## Configuration

- Directory: `/etc/pve2netbox/`
- Variables file: `/etc/pve2netbox/env` — format `KEY=value`, no `export`. Mode samples: **env.single-run**, **env.simple-mode**, **env.combined-mode** in this directory (copy: `sudo cp env.<mode> /etc/pve2netbox/env`). Or use [.env.example](../../.env.example) as template.

```bash
sudo mkdir -p /etc/pve2netbox
sudo cp env.simple-mode /etc/pve2netbox/env   # or env.single-run / env.combined-mode
sudo nano /etc/pve2netbox/env
```

Install unit files:

```bash
sudo cp pve2netbox.service /etc/systemd/system/
sudo systemctl daemon-reload
```

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

## Operation modes

| Mode | How to run |
|------|------------|
| **1. Single-run** | Run manually: `python3 -m pve2netbox` or `pve2netbox` (env from file or export). Or cron / systemd timer (see option B below). |
| **2. Simple** | Long-running service: set only `SYNC_INTERVAL_SECONDS` in `env`. `systemctl enable --now pve2netbox`. |
| **3. Combined** | In `env` set `QUICK_CHECK_INTERVAL_SECONDS` and optionally `SYNC_INTERVAL_SECONDS`. `systemctl enable --now pve2netbox`. |

## Run

**Option A — long-running service (modes 2 and 3):**

```bash
sudo systemctl enable --now pve2netbox
```

Service runs continuously; restart on failure after 60s.

**Option B — on timer (mode 2 “on schedule”):** one sync per run, timer every 5 minutes.

```bash
sudo cp pve2netbox-oneshot.service pve2netbox.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pve2netbox.timer
```

Timer interval: edit `OnUnitActiveSec=5min` in `pve2netbox.timer`.

## Check

```bash
sudo systemctl status pve2netbox
journalctl -u pve2netbox -f
```
