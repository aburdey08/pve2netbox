# pve2netbox

Synchronize **Proxmox VE** (PVE) VM and LXC inventory to **NetBox**. Single-run or continuous sync with configurable intervals.

Based on [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync).

## How it works

The app talks to the Proxmox VE API, fetches VM/LXC data, and creates/updates (optionally deletes) resources in NetBox. Custom fields and device roles can be created automatically on first run.

## Quick start

```bash
docker run -d --name pve2netbox \
  -e PVE_API_HOST=10.10.0.10 \
  -e PVE_API_USER=netsync@pve \
  -e PVE_API_TOKEN=your-token-name \
  -e PVE_API_SECRET=your-token-secret \
  -e NB_API_URL=https://netbox.example.org \
  -e NB_API_TOKEN=your-netbox-token \
  -e SYNC_INTERVAL_SECONDS=300 \
  username/pve2netbox:latest
```

Replace `username` with your Docker Hub username.

## Operation modes

| Mode | Behavior |
|------|----------|
| **Single-run** | One sync, then exit. Do not set `SYNC_INTERVAL_SECONDS` or `QUICK_CHECK_INTERVAL_SECONDS`. |
| **Simple** | Full sync every N seconds. Set `SYNC_INTERVAL_SECONDS=300` (or desired value). Suited for smaller setups. |
| **Combined** | Quick check every minute; full sync every hour. Set `QUICK_CHECK_INTERVAL_SECONDS=60` and `SYNC_INTERVAL_SECONDS=3600`. Recommended for larger setups. |

## Required environment variables

| Variable | Description |
|----------|-------------|
| **PVE_API_HOST** | DNS or IP of your Proxmox VE host |
| **PVE_API_USER** | PVE user (e.g. `netsync@pve`) |
| **PVE_API_TOKEN** | PVE API token name |
| **PVE_API_SECRET** | PVE API token secret |
| **NB_API_URL** | NetBox URL (e.g. `https://netbox.example.org`) |
| **NB_API_TOKEN** | NetBox API token |

**Proxmox:** Create a user with Pool.Audit, VM.Audit, Sys.Audit and an API token.  
**NetBox:** Create a user with write access and use its API token.

## Optional environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| PVE_API_VERIFY_SSL | true | PVE SSL verification (`true`/`false`) |
| NB_CLUSTER_ID | — | NetBox cluster ID |
| NB_API_DELAY_SECONDS | 0.2 | Delay between NetBox API requests (increase if 502 errors) |
| NB_API_RETRY_TOTAL | 5 | Retries on 502/503/429 |
| SYNC_INTERVAL_SECONDS | — | Full sync interval in seconds (modes 2 and 3) |
| QUICK_CHECK_INTERVAL_SECONDS | — | Quick check interval in seconds (mode 3) |
| SYNC_VMS | true | Sync QEMU VMs |
| SYNC_LXC | true | Sync LXC containers |
| VM_ROLE | — | NetBox device role for VMs (e.g. `Virtual Machine`) |
| LXC_ROLE | — | NetBox device role for LXC (e.g. `Container`) |
| DRY_RUN | false | No changes in NetBox, sync simulation only |
| ENABLE_CLEANUP | false | Remove from NetBox VMs/LXC that no longer exist in PVE (use with care) |
| LOG_LEVEL | INFO | DEBUG, INFO, WARNING, ERROR |
| ENABLE_METRICS | false | Expose Prometheus metrics |
| METRICS_PORT | 9090 | Metrics port (expose with `-p 9090:9090`) |

## Using with Docker Compose

```yaml
services:
  pve2netbox:
    image: username/pve2netbox:latest
    environment:
      - PVE_API_HOST=10.10.0.10
      - PVE_API_USER=netsync@pve
      - PVE_API_TOKEN=your-token
      - PVE_API_SECRET=your-secret
      - NB_API_URL=https://netbox.example.org
      - NB_API_TOKEN=your-netbox-token
      - SYNC_INTERVAL_SECONDS=300
    ports:
      - "9090:9090"
    restart: unless-stopped
```

Expose port 9090 if you set `ENABLE_METRICS=true`.

## Prometheus metrics

Set `ENABLE_METRICS=true` and expose port 9090. Metrics at `http://localhost:9090/metrics` include sync counts, errors, and last sync duration.

## Supported resources

- **QEMU/KVM VMs:** disks, network interfaces (VLAN, MTU), IPs via QEMU Guest Agent (MAC matching).
- **LXC:** rootfs, mount points, network interfaces. IPs for LXC are not synced.

Custom fields (autostart, replicated, ha, backup, dns_name) and device roles can be created automatically on first run.

## Documentation

- **Source & full docs:** https://github.com/aburdey08/pve2netbox
- **Changelog:** https://github.com/aburdey08/pve2netbox/blob/master/Changelog.md
