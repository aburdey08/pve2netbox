# Docker: pve2netbox

Run inside a Docker container. Three modes — three docker-compose files; parameters are set in each file.

## Files

| Mode | docker-compose |
|------|----------------|
| **1. Single-run** | `docker-compose.single-run.yml` |
| **2. Simple** | `docker-compose.simple-mode.yml` |
| **3. Combined** | `docker-compose.combined-mode.yml` |

## Configuration

Open the desired compose file and edit the `environment` block: set your `PVE_API_*`, `NB_API_*`, and optionally intervals (`SYNC_INTERVAL_SECONDS`, `QUICK_CHECK_INTERVAL_SECONDS`).

### Environment variables

| Variable | Required | Description |
|----------|-----------|-------------|
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

## Run

Run all commands **from the repository root**.

| Mode | Command |
|------|---------|
| **1. Single-run** | `docker compose -f contrib/docker/docker-compose.single-run.yml run --rm pve2netbox` |
| **2. Simple** | `docker compose -f contrib/docker/docker-compose.simple-mode.yml up -d --build` |
| **3. Combined** | `docker compose -f contrib/docker/docker-compose.combined-mode.yml up -d --build` |

## Check

- Logs: `docker compose -f contrib/docker/docker-compose.<mode>.yml logs -f pve2netbox`
- Metrics (if enabled): `http://localhost:9090/metrics`
