# pve2netbox

Sync **Proxmox VE** (QEMU VMs + LXC containers) inventory to **NetBox** — one-shot or continuous with configurable intervals.

- **Source & full docs:** https://github.com/aburdey08/pve2netbox
- **Changelog:** https://github.com/aburdey08/pve2netbox/blob/master/Changelog.md
- Based on [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync)

---

## Quick start — Combined mode (recommended)

Quick change-check every minute + full sync every hour. Best balance for most setups.

```yaml
# docker-compose.yml
services:
  pve2netbox:
    image: aburdey/pve2netbox:1.0.8
    environment:
      # Proxmox VE API (required)
      - PVE_API_HOST=10.10.0.10
      - PVE_API_USER=netsync@pve
      - PVE_API_TOKEN=your-token-name
      - PVE_API_SECRET=your-token-secret
      # NetBox API (required)
      - NB_API_URL=https://netbox.example.org
      - NB_API_TOKEN=your-netbox-token
      # Combined mode
      - QUICK_CHECK_INTERVAL_SECONDS=60
      - SYNC_INTERVAL_SECONDS=3600
      # Optional
      - VM_ROLE=Virtual Machine
      - LXC_ROLE=Container
    ports:
      - "9090:9090"   # only needed if ENABLE_METRICS=true
    restart: unless-stopped
```

```bash
docker compose up -d
docker compose logs -f
```

### Or with `docker run`

```bash
docker run -d --name pve2netbox --restart unless-stopped \
  -e PVE_API_HOST=10.10.0.10 \
  -e PVE_API_USER=netsync@pve \
  -e PVE_API_TOKEN=your-token-name \
  -e PVE_API_SECRET=your-token-secret \
  -e NB_API_URL=https://netbox.example.org \
  -e NB_API_TOKEN=your-netbox-token \
  -e QUICK_CHECK_INTERVAL_SECONDS=60 \
  -e SYNC_INTERVAL_SECONDS=3600 \
  aburdey/pve2netbox:1.0.5
```

---

## Prerequisites

- **Proxmox VE:** user with `Pool.Audit`, `VM.Audit`, `Sys.Audit` and an API token.
- **NetBox:** user with write access and an API token.
- In NetBox: create the physical nodes with names **matching** Proxmox node names.

---

## Operation modes

Mode is selected by which interval variables you set:

| Mode | Variables | Behavior |
|------|-----------|----------|
| **Single-run** | none | One sync, then exit (use `docker compose run --rm pve2netbox`) |
| **Simple** | `SYNC_INTERVAL_SECONDS=300` | Full sync every N seconds |
| **Combined** (recommended) | `QUICK_CHECK_INTERVAL_SECONDS=60` + `SYNC_INTERVAL_SECONDS=3600` | Quick check every minute + full sync every hour |

Repeated syncs only create/update — they never wipe unrelated NetBox data.

---

## Environment variables

**Required:**

| Variable | Description |
|----------|-------------|
| `PVE_API_HOST` | Proxmox host (DNS or IP) |
| `PVE_API_USER` | PVE user (e.g. `netsync@pve`) |
| `PVE_API_TOKEN` | PVE API token name |
| `PVE_API_SECRET` | PVE API token secret |
| `NB_API_URL` | NetBox URL (e.g. `https://netbox.example.org`) |
| `NB_API_TOKEN` | NetBox API token |
| `NB_CLUSTER_ID` **or** `NB_CLUSTER_NAME` | Target NetBox cluster — an existing ID, or a name that is created if missing |

**Optional:**

| Variable | Default | Description |
|----------|---------|-------------|
| `PVE_API_VERIFY_SSL` | `false` | Verify PVE SSL cert. **Changes to `true` in 2.0.0** — set it explicitly now |
| `NB_API_DELAY_SECONDS` | `0.2` | Delay between NetBox requests (raise on 502 errors) |
| `NB_API_RETRY_TOTAL` | `5` | Retries on 502/503/429 |
| `NB_API_RETRY_BACKOFF` | `1.0` | Backoff factor between retries |
| `SYNC_VMS` / `SYNC_LXC` / `SYNC_TAGS` | `true` | Enable/disable each sync type |
| `VM_ROLE` / `LXC_ROLE` | — | NetBox device role for VMs / LXC (created if missing) |
| `NODE_MISSING_POLICY` | `skip` | Proxmox node with no matching NetBox device: `skip` or `fail` |
| `DRY_RUN` | `false` | **Partial** — provisioning, node status and cleanup only; VM/interface/IP/disk records are still written |
| `ENABLE_CLEANUP` | `false` | Delete from NetBox VMs missing in PVE (**use with care**) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENABLE_METRICS` | `false` | Expose Prometheus metrics on `/metrics` |
| `ENABLE_HEALTH_ENDPOINT` | `true` | Expose `/healthz` and `/readyz` (used by the image healthcheck) |
| `METRICS_PORT` | `9090` | Port for `/metrics`, `/healthz`, `/readyz` (expose with `-p 9090:9090`) |

---

## What gets synced

- **QEMU/KVM VMs** — disks (SCSI/SATA/VirtIO/IDE/EFI), NICs with VLAN and MTU, IPs via **QEMU Guest Agent** (interfaces matched by MAC).
- **LXC containers** — rootfs and mount points (`mp0`, `mp1`…), NICs with MTU. IP sync not available (no guest agent).
- **Tags** from Proxmox — synced to NetBox VMs (created automatically if missing).

**Auto-created in NetBox on first run:** custom fields `autostart`, `replicated`, `ha` (Virtual Machine), `backup` (Virtual Disk), `dns_name` (Prefix); device roles from `VM_ROLE` / `LXC_ROLE`.

---

## Prometheus metrics and health

One HTTP server on `METRICS_PORT` serves `/metrics` (when `ENABLE_METRICS=true`), plus `/healthz`
and `/readyz` (on by default). The image's `HEALTHCHECK` polls `/readyz`, which returns 503 while
no full sync has succeeded or the last successful one is older than two sync intervals — a container
whose syncs are failing shows up as `unhealthy`.

`pve2netbox_build_info`, `pve2netbox_full_syncs_total`, `pve2netbox_quick_checks_total`,
`pve2netbox_vms_synced_total`, `pve2netbox_lxc_synced_total`, `pve2netbox_errors_total`,
`pve2netbox_vms_tracked`, `pve2netbox_lxc_tracked`, `pve2netbox_last_sync_duration_seconds`,
`pve2netbox_last_sync_timestamp_seconds`, `pve2netbox_last_success_timestamp_seconds`,
`pve2netbox_changes_detected`.

`SIGTERM` is handled gracefully: `docker stop` finishes the object being written and exits cleanly.

---

## Other install options

Running outside Docker? See the project repo:

- **LXC on Proxmox** — one-command deploy script: [`contrib/lxc/`](https://github.com/aburdey08/pve2netbox/tree/master/contrib/lxc)
- **systemd on host / bare LXC**: [`contrib/systemd/`](https://github.com/aburdey08/pve2netbox/tree/master/contrib/systemd)
- **pip from source**: `pip install .` in repo root, then run `pve2netbox`
