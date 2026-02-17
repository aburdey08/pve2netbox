# pve2netbox
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-0db7ed?logo=docker&logoColor=white)](https://hub.docker.com/r/aburdey/pve2netbox)


Yes, another Proxmox Virtual Environment (PVE) inventory sync to NetBox. Based on [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync). I liked its approach and initially planned to fix a few issues, but it grew into a small standalone project.

## How it works

The application talks to the Proxmox VE API, fetches VM/LXC data, and creates/updates/deletes resources in NetBox.

## Installation

The following options are available:

| Method | Configuration | Documentation |
|--------|---------------|---------------|
| **pip3** (local build) | Environment variables or `.env` | see below |
| **Docker** — **Recommended!** | Parameters in compose files `contrib/docker/docker-compose.*.yml` | [contrib/docker/](contrib/docker/) |
| **LXC** — **Recommended!** | `/etc/pve2netbox/env` or `.env` | [contrib/lxc/](contrib/lxc/) |
| **systemd** (host/LXC without Docker) | `/etc/pve2netbox/env` | [contrib/systemd/](contrib/systemd/) |

Quick start and variable descriptions are in each category per the table above.

## Configuration

### NetBox

Create a dedicated user (e.g. pve2netbox) in NetBox and grant an API token with write access.

Set the following environment variables:

- **NB_API_URL**: URL of your NetBox instance (e.g. https://netbox.example.org)
- **NB_API_TOKEN**: The token created above

Optionally set **NB_CLUSTER_ID** — the cluster ID to use in NetBox.

To avoid overloading NetBox (e.g. 502 errors when syncing too often), you can set:

- **NB_API_DELAY_SECONDS**: Delay in seconds between NetBox API requests (default: `0.2`). Increase to `0.5` or `1.0` if 502 errors persist.
- **NB_API_RETRY_TOTAL**: Number of retries on 502/503/429 (default: `5`).
- **NB_API_RETRY_BACKOFF**: Backoff factor in seconds between retries (default: `1.0`).

Selective sync:

- **SYNC_VMS**: Sync QEMU VMs (default: `true`).
- **SYNC_LXC**: Sync LXC containers (default: `true`).

Device roles (optional):

- **VM_ROLE**: Name or ID of the NetBox device role for QEMU VMs (e.g. `Virtual Machine`). The role must exist in NetBox.
- **LXC_ROLE**: Name or ID of the NetBox device role for LXC containers (e.g. `Container`). The role must exist in NetBox.

Minimum setup in NetBox:

- Create the physical nodes that host the cluster (names must match Proxmox so the app can link VMs to hosts).
- Create the cluster.

### Auto-created custom fields in NetBox

On first run, the following custom fields are created:

| Name       | Object types   | Label      | Type    | Description                    |
|------------|----------------|------------|---------|--------------------------------|
| autostart  | Virtual Machine| Autostart  | Boolean | VM autostart on boot           |
| replicated | Virtual Machine| Replicated | Boolean | VM replication enabled          |
| ha         | Virtual Machine| Failover   | Boolean | VM high availability enabled   |
| backup     | Virtual Disk   | Backup     | Boolean | Disk backup enabled             |
| dns_name   | Prefix         | DNS Name   | Text    | DNS domain name for the prefix |

### Auto-created device roles

If **VM_ROLE** or **LXC_ROLE** are set, the app creates those roles in NetBox when missing:

- **VM_ROLE** (e.g. "Virtual Machine")
- **LXC_ROLE** (e.g. "Container")

Both are set as `vm_role=true` for use with virtual machines.

### Proxmox VE API

Create a dedicated user (e.g. netsync) in the PVE cluster and grant an API token.

The user must have Pool.Audit, VM.Audit, Sys.Audit.

Set the following environment variables:

- **PVE_API_HOST**: DNS name or IP of your PVE (e.g. 10.10.0.10)
- **PVE_API_USER**: Username of the account (e.g. netsync@pve)
- **PVE_API_TOKEN**: API token name (e.g. test-token)
- **PVE_API_SECRET**: API token secret

## Run

This section describes local run via pip3. Using an LXC container or Docker is recommended. For completeness, run from source with pip3 and systemd are also documented for those who prefer them.

1. **Install dependencies and package** (once, from repo root):

```bash
cd /path/to/pve2netbox
pip3 install .       # or pip install -e . for development
```

Dependencies will be installed from [pyproject.toml](pyproject.toml) and the `pve2netbox` command will be available.

2. **Set environment variables** — use a `.env` file in the project directory or follow [.env.example](.env.example).

3. **Run:**

```bash
pve2netbox
```

Or without installing to PATH (from repo root after step 1): `python -m pve2netbox` or `python3 -m pve2netbox`.

Commands for LXC, Docker, and systemd are in [Deploy and run](#deploy-and-run).

## Other options

### Dry-run mode

Run sync without making changes in NetBox:

```bash
DRY_RUN=true
```

### Automatic cleanup

Remove from NetBox VMs that no longer exist in Proxmox:

```bash
ENABLE_CLEANUP=true
```

⚠️ **Warning:** use with care — VMs/LXC missing in Proxmox VE will be deleted from NetBox.

### Prometheus metrics

Export metrics for Prometheus/Grafana:

```bash
ENABLE_METRICS=true 
METRICS_PORT=9090
```

Metrics are exposed at `http://localhost:9090/metrics`:

- `pve2netbox_full_syncs_total` — total full syncs
- `pve2netbox_quick_checks_total` — total quick checks
- `pve2netbox_vms_synced_total` — total VMs synced
- `pve2netbox_lxc_synced_total` — total LXC containers synced
- `pve2netbox_errors_total` — total errors
- `pve2netbox_vms_tracked` — current number of tracked VMs
- `pve2netbox_last_sync_duration_seconds` — last sync duration

### Log level

Control log verbosity:

```bash
LOG_LEVEL=DEBUG pve2netbox  # Options: DEBUG, INFO, WARNING, ERROR
```

## Deploy and run

Below: operation modes (shared by Docker, LXC, systemd), then deployment options — Docker, LXC, systemd.

### Operation modes (shared by Docker, LXC, systemd)

Interval variables are set in config (`.env` or `/etc/pve2netbox/env`):

| Mode            | Variables | Behavior |
|-----------------|-----------|----------|
| **1. Single-run**   | Do not set `SYNC_INTERVAL_SECONDS` and `QUICK_CHECK_INTERVAL_SECONDS` | One sync run, then exit. |
| **2. Simple**       | Only `SYNC_INTERVAL_SECONDS` (e.g. `300`) | Full sync every N seconds. Suitable for small setups. |
| **3. Combined**     | `QUICK_CHECK_INTERVAL_SECONDS=60`, `SYNC_INTERVAL_SECONDS=3600` | Quick check every minute; on changes, sync only changed VMs; full sync every hour. Recommended for larger setups. |

**General:** repeated syncs do not overwrite extra data in NetBox — only create/update according to current PVE state.

### Docker

Three compose files per mode: [contrib/docker/](contrib/docker/). Run from repo root with `-f contrib/docker/docker-compose.<mode>.yml`.

### LXC (including Proxmox)

One-command deploy from a PVE host: [contrib/lxc/](contrib/lxc/) (script `deploy-from-pve.sh` creates the container and installs the app).

### Configuration

Variables as in [.env.example](.env.example). For systemd inside LXC: file `/etc/pve2netbox/env` (format `KEY=value`, no `export`).

### Install in LXC

Python 3.8+, pip, then from source `pip install .`. Or one-shot install: `curl -sL …/contrib/lxc/install.sh | sudo bash` (see [contrib/lxc/](contrib/lxc/)).

### Run by mode

- **Mode 1:** run `pve2netbox` manually or via cron (variables in env file or `export`).
- **Mode 2 or 3:** unit files from [contrib/systemd/](contrib/systemd/): `systemctl enable --now pve2netbox`.

### Check

`systemctl status pve2netbox`, `journalctl -u pve2netbox -f`.

### Network

From the container, `PVE_API_HOST` and `NB_API_URL` must be reachable.

## Supported virtualization types

The app syncs **QEMU/KVM virtual machines** and **LXC containers** from Proxmox VE:

- **QEMU VMs**: full support, including disks (SCSI/SATA/VirtIO/IDE/EFI), network interfaces with VLAN tags and MTU, IP addresses via QEMU Guest Agent with interface matching by MAC.
- **LXC containers**: rootfs and mount points (mp0, mp1, …), network interfaces with MTU. IP addresses for LXC are not synced (no guest agent).

### QEMU Guest Agent

The app detects whether QEMU Guest Agent is enabled for a VM (`agent=1` in VM config). When the agent is enabled and the VM is running:

- **Interface names**: real names from the guest OS (e.g. `eth0`, `ens18`) are used instead of Proxmox config keys (`net0`, `net1`)
- **MAC matching**: network interfaces are matched by MAC between Proxmox config and guest agent data
- **IP addresses**: IPv4/IPv6 with correct prefixes are synced to the corresponding NetBox interface
- Only interfaces with matching MAC receive IP assignment and name updates from the agent

To disable sync for one type, set in `.env`: `SYNC_VMS=false` or `SYNC_LXC=false`.
