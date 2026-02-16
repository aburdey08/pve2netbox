# pve2netbox

## [1.0.0] - 2026-02-14

Project renamed to **pve2netbox** (formerly a fork of [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync) v0.2.4) with extended functionality.

### Added

#### Monitoring and observability

- **Prometheus metrics** (`ENABLE_METRICS=true`)
  - HTTP endpoint `/metrics` on port 9090
  - Metrics: syncs, VM/LXC counts, errors, duration
- **Structured logging**
  - Levels: DEBUG, INFO, WARNING, ERROR (via `LOG_LEVEL`)
  - Formatted output with timestamps
  - Context for debugging

#### New features

- **Dry-run mode** (`DRY_RUN=true`) — test without making changes
- **Automatic cleanup** (`ENABLE_CLEANUP=true`) — remove VMs from NetBox that no longer exist
- **Configuration validation** — check required variables at startup
- **LXC container support** (`SYNC_LXC`, `LXC_ROLE`)
  - Sync rootfs, mount points, network interfaces
  - Separate roles for VM and LXC
- **Sync modes**
  - Single-run: one-off sync
  - Simple mode: periodic full sync (`SYNC_INTERVAL_SECONDS`)
  - Combined mode: quick checks + full sync (`QUICK_CHECK_INTERVAL_SECONDS`)

#### Docker

- `contrib/docker/`: `Dockerfile` with health checks, `docker-compose.yml` with full config
- Ready env samples in `contrib/docker/`: `env.single-run`, `env.simple-mode`, `env.combined-mode`

#### Extended Proxmox integration

- **QEMU Guest Agent**
  - Real interface names (eth0, ens18) instead of net0/net1
  - MAC-based matching
  - Correct IP assignment
- **Auto-provisioning** — automatic creation of custom fields and device roles
- **MTU support** for network interfaces
- **Extended disk support**
  - Types: SCSI, SATA, VirtIO, IDE, EFI
  - Sizes: K/M/G/T
- **Conflict handling**
  - MAC/IP: auto-reassign when VM is offline
  - VRF support for IP addresses
  - Detailed errors on real conflicts

### Improved

- **Performance**: batch loading from NetBox (~20–30% faster)
- **Reliability**: rate limiting and retry on 502/503/429 (`NB_API_DELAY_SECONDS`, `NB_API_RETRY_*`)
- **Error handling**: contextual messages and graceful degradation
- **Architecture**: modular layout with type hints

### Technical details

Five new environment variables: `DRY_RUN`, `ENABLE_CLEANUP`, `LOG_LEVEL`, `ENABLE_METRICS`, `METRICS_PORT`

---

## Upstream: [0.2.4] - 02/09/2025

### Added (from previous unreleased section)

- **Combined sync mode** (`QUICK_CHECK_INTERVAL_SECONDS`):
  - Quick VM change check every minute (without loading configs)
  - Incremental sync of only changed VMs
  - Full sync every hour for consistency
  - Minimal load on Proxmox and NetBox APIs
  - Recommended for setups with >50 VMs
- `quick_check_changes()` — quick check of VM state (status, name, node, memory, disk)
- `_load_specific_objects()` — load only needed objects from NetBox for incremental sync
- `sync_specific_vms()` — sync only specific changed VMs
- **LXC container sync** (rootfs, mount points, network interfaces).
- Configurable virtualization types: `SYNC_VMS` and `SYNC_LXC` (both on by default).
- **Role configuration**: `VM_ROLE` and `LXC_ROLE` for NetBox Device Roles for QEMU VMs and LXC.
- **Auto-provisioning**: custom fields (autostart, replicated, ha, backup, dns_name) and device roles created in NetBox on first run.
- **MTU support** for VM and LXC network interfaces: MTU from Proxmox synced to NetBox.
- **Improved QEMU Guest Agent**: detect agent (VM running), match interfaces by MAC, use real names (eth0, ens18) instead of net0/net1, correct IP assignment.
- All QEMU disk types: SCSI, SATA, VirtIO, IDE, EFI (`efidisk0`).
- Disk sizes in kilobytes (K), megabytes (M), gigabytes (G), terabytes (T).
- Detailed sync-stage logging for debugging.

### Fixed

- **Duplicate network interfaces** when VM is stopped: look up existing interface by MAC before creating. No more duplicate interfaces (net0 vs eth0) when toggling with/without guest agent.
- **MAC conflict handling** when cloning VMs:
  - On duplicate MAC, check old VM status
  - If old VM **offline** → MAC reassigned to new VM (clear primary on old, set on new)
  - If both **active** → ERROR with conflict details, sync skipped without crash
  - Avoids NetBox error: *"Cannot reassign MAC Address while it is designated as the primary MAC for an object"*
- **IP conflict handling** when cloning VMs:
  - **VRF support**: IPs in different VRF are not a conflict — create new IP in current VRF
  - On duplicate IP in same VRF, check old VM status
  - If old VM **offline** → IP reassigned to new VM (clear primary_ip4/primary_ip6 on old)
  - If both **active** → ERROR with details (VM name, ID, status, interface), sync skipped
  - **VM status check**: correct NetBox status handling (case-insensitive, "Offline" format)
  - Avoids NetBox error: *"Cannot reassign IP address while it is designated as the primary IP for the parent object"*
- **Node status in `sync_specific_vms()`**: use correct Proxmox node status source.
- Reduced NetBox API load: delay between requests (`NB_API_DELAY_SECONDS`) and retries on 502/503/429 (`NB_API_RETRY_*`).
- Excluded PVE system devices (TPM, scsihw, ide2) from disk sync.
- MAC address no longer added to interface Description.

## [0.2.4] - 02/09/2025

### Fixed

- Import vCPU core count instead of total available cores.

## [0.2.3] - 26/08/2025

### Fixed

- Allow VM disk sizes in Megabytes.

## [0.2.2] - 07/05/2025

### Fixed

- Use `NB_CLUSTER_ID` even for VM update.

## [0.2.1] - 01/05/2025

### Fixed

- [#7] Improve tag handling.

## [0.2.0] - 21/04/2025

### New

- Monitoring PVE HA/Replication.

## [0.1.1] - 19/02/2025

### New

- [#5] Allow to configure cluster ID.

### Changed

- Add eth0 as raw_interface_name.

## [0.1.0] - 19/02/2025

- Initial release.
