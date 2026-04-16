# pve2netbox

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-0db7ed?logo=docker&logoColor=white)](https://hub.docker.com/r/aburdey/pve2netbox)

Sync Proxmox VE (PVE) inventory to NetBox: QEMU VMs and LXC containers, their disks, network interfaces, IPs (via QEMU Guest Agent) and tags.

Based on [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync).

---

## Quick start

Pick **one** of the two recommended options. Both use **Combined mode** — quick change-check every minute, full sync every hour (best balance for most setups).

Before you start, you need:

- A **NetBox** API token with write access and (optionally) `NB_CLUSTER_ID`.
- A **Proxmox VE** user + API token with `Pool.Audit`, `VM.Audit`, `Sys.Audit`.
- Physical nodes already created in NetBox with names **matching** Proxmox node names.

### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/aburdey08/pve2netbox.git
cd pve2netbox

# 1. Edit credentials (PVE_API_*, NB_API_*) in the compose file:
$EDITOR contrib/docker/docker-compose.combined-mode.yml

# 2. Start:
docker compose -f contrib/docker/docker-compose.combined-mode.yml up -d

# 3. Watch logs:
docker compose -f contrib/docker/docker-compose.combined-mode.yml logs -f
```

That's it. More details and other modes: [contrib/docker/](contrib/docker/).

### Option B — LXC on Proxmox (one command)

Run **on a Proxmox node as root** — creates a Debian 12 LXC and installs everything. If a **`.env`** file is placed next to the script, it is copied into the container as `/etc/pve2netbox/env` automatically — the service is fully configured right after deploy.

```bash
# 1. Get the Combined-mode sample and the deploy script:
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/env.combined-mode -o .env
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/deploy-from-pve.sh -o deploy-from-pve.sh
chmod +x deploy-from-pve.sh

# 2. Fill in PVE_API_* and NB_API_* in .env:
$EDITOR .env

# 3. Deploy (interactively asks for CTID, hostname, storage):
./deploy-from-pve.sh

# 4. Enable the service:
pct exec <CTID> -- systemctl enable --now pve2netbox
```

Skip step 1–2 if you prefer to edit `/etc/pve2netbox/env` inside the container after deploy.

More details: [contrib/lxc/](contrib/lxc/).

---

## Configuration

Minimum required variables — set these in the compose file or `/etc/pve2netbox/env`:

| Variable | Description |
|----------|-------------|
| `PVE_API_HOST` | Proxmox host (DNS or IP) |
| `PVE_API_USER` | PVE user (e.g. `netsync@pve`) |
| `PVE_API_TOKEN` | PVE API token name |
| `PVE_API_SECRET` | PVE API token secret |
| `NB_API_URL` | NetBox URL (e.g. `https://netbox.example.org`) |
| `NB_API_TOKEN` | NetBox API token |

Common optional variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NB_CLUSTER_ID` | — | NetBox cluster ID |
| `PVE_API_VERIFY_SSL` | `true` | Verify PVE SSL cert |
| `VM_ROLE` / `LXC_ROLE` | — | NetBox device role for VMs / LXC (created if missing) |
| `SYNC_VMS` / `SYNC_LXC` / `SYNC_TAGS` | `true` | Enable/disable each sync type |
| `DRY_RUN` | `false` | Log changes without writing to NetBox |
| `ENABLE_CLEANUP` | `false` | Delete from NetBox VMs missing in PVE (**use with care**) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENABLE_METRICS` / `METRICS_PORT` | `false` / `9090` | Prometheus metrics on `/metrics` |

Full list and comments: [.env.example](.env.example).

---

## Operation modes

Interval variables pick the mode:

| Mode | Variables | Behavior |
|------|-----------|----------|
| **Single-run** | none | One sync, then exit |
| **Simple** | `SYNC_INTERVAL_SECONDS=300` | Full sync every N seconds |
| **Combined** (recommended) | `QUICK_CHECK_INTERVAL_SECONDS=60` + `SYNC_INTERVAL_SECONDS=3600` | Quick change-check every minute + full sync every hour |

Repeated syncs only create/update — they never wipe unrelated NetBox data.

---

## Other install options

For special cases — see dedicated docs:

- **Docker** (all three modes): [contrib/docker/](contrib/docker/)
- **LXC** (deploy, update, install into existing container): [contrib/lxc/](contrib/lxc/)
- **systemd** on host / bare LXC: [contrib/systemd/](contrib/systemd/)
- **pip3 from source** — `pip install .` in repo root, then run `pve2netbox` with a `.env` file

---

## How it works

Hits the Proxmox VE API, reads VMs/LXC, and creates/updates NetBox objects accordingly.

**Supported:**

- **QEMU VMs** — disks (SCSI/SATA/VirtIO/IDE/EFI), NICs with VLAN and MTU, IPs via QEMU Guest Agent (interfaces matched by MAC).
- **LXC containers** — rootfs and mount points (`mp0`, `mp1`…), NICs with MTU. IP sync not available (no guest agent).

**QEMU Guest Agent** (when `agent=1` and VM is running): real OS interface names (e.g. `eth0`) instead of `net0`, MAC-based matching, IPv4/IPv6 assignment to NetBox interfaces.

**Auto-created in NetBox on first run:**

| Object | Name | Type | Scope |
|--------|------|------|-------|
| Custom field | `autostart` | Boolean | Virtual Machine |
| Custom field | `replicated` | Boolean | Virtual Machine |
| Custom field | `ha` | Boolean | Virtual Machine |
| Custom field | `backup` | Boolean | Virtual Disk |
| Custom field | `dns_name` | Text | Prefix |
| Device role | from `VM_ROLE` / `LXC_ROLE` | — | `vm_role=true` |

---

## Prometheus metrics

Set `ENABLE_METRICS=true` (port `METRICS_PORT`, default `9090`) — exposes at `http://host:9090/metrics`:

`pve2netbox_full_syncs_total`, `pve2netbox_quick_checks_total`, `pve2netbox_vms_synced_total`, `pve2netbox_lxc_synced_total`, `pve2netbox_errors_total`, `pve2netbox_vms_tracked`, `pve2netbox_last_sync_duration_seconds`.

---

## Tuning NetBox load

If NetBox returns 502s under load:

- `NB_API_DELAY_SECONDS` — delay between requests (default `0.2`; try `0.5`–`1.0`).
- `NB_API_RETRY_TOTAL` — retries on 502/503/429 (default `5`).
- `NB_API_RETRY_BACKOFF` — backoff factor (default `1.0`).
