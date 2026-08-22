# pve2netbox

## [1.0.8] - 2026-08-22

Reliability release: the documented installation methods work again, and the daemon starts, runs
and stops correctly.

### Fixed

- **Console script `pve2netbox` was broken.** `pyproject.toml` pointed the entry point at a
  non-existent `pve2netbox:master`, so `pip install .` produced a command that failed with
  `AttributeError`. Mode dispatch also lived under `if __name__ == '__main__'` in `__main__.py`,
  so the console script could never have run the interval modes. All mode handling moved to the
  new `pve2netbox/cli.py`; `pve2netbox` and `python -m pve2netbox` now behave identically.
- **`.env` files were never read.** The README documented running `pve2netbox` with a `.env` file,
  but nothing in the code loaded one. Added `--env-file PATH`, `$PVE2NETBOX_ENV_FILE` and
  auto-discovery of `./.env`. Variables already present in the environment win over the file, so
  Docker and systemd values are never shadowed.
- **`NB_CLUSTER_ID` silently defaulted to cluster `1`.** The value was read directly from the
  environment in six places, bypassing the parsed configuration; a missing or wrong ID surfaced as
  an opaque NetBox API error on the first VM. The cluster is now validated at startup, and
  configuration is read in exactly one place.
- **A Proxmox node without a matching NetBox device killed the process** via `sys.exit(1)` in the
  middle of a sync. Controlled by the new `NODE_MISSING_POLICY` (default `skip`).
- **`requires-python` claimed 3.8**, but PEP 585 annotations evaluated at import time made the
  package unimportable there. The real minimum, 3.9, is now declared.
- **The Docker `HEALTHCHECK` was a no-op** (`python -c "import sys; sys.exit(0)"`) and reported
  every container as healthy. It now queries `/readyz`.
- **A NetBox outage at startup crashed the process**; the looping modes now retry until NetBox
  answers or a shutdown is requested.
- **Quick check silently fell back to a legacy implementation** on any exception — including
  ordinary network errors — and that implementation ignored `IGNORE_STATUS_WHEN_LOCKED`,
  reintroducing the changelog noise fixed in 1.0.6. There is now a single implementation.

### Added

- **`NB_CLUSTER_NAME`** — target the NetBox cluster by name instead of by ID; created together with
  the `Proxmox VE` cluster type when missing. Any existing cluster ID keeps working — the removed
  hard-coded `1` was the bug. Setting both `NB_CLUSTER_ID` and `NB_CLUSTER_NAME` is allowed only
  when they refer to the same cluster; a mismatch is a startup error instead of a silent choice.
- **`NODE_MISSING_POLICY`** (`skip` | `fail`, default `skip`) — what to do when a Proxmox node has
  no matching device in NetBox.
- **`ENABLE_HEALTH_ENDPOINT`** (default `true`) — `/healthz` (liveness) and `/readyz` (readiness,
  503 while the last successful full sync is older than two sync intervals) on `METRICS_PORT`,
  served whether or not `/metrics` is enabled.
- **Graceful shutdown.** SIGTERM and SIGINT stop the run at the next node or VM boundary and exit
  with code 0, instead of being killed mid-write after the container stop timeout. A second signal
  exits immediately. An interrupted run never performs `ENABLE_CLEANUP` deletions.
- **`--version`, `--help`, `--env-file`** on the command line, plus the
  `pve2netbox_build_info{version=...}` metric.
- **`pve2netbox_last_success_timestamp_seconds`** — timestamp of the last *successful* full sync,
  separate from `pve2netbox_last_sync_timestamp_seconds`, which now marks the last attempt. The
  Grafana dashboard's "Last sync" panel uses the new metric.
- Startup now logs the effective configuration (no secrets) and warns that `PVE_API_VERIFY_SSL`
  is disabled.

### Changed

- **Documentation fix:** `PVE_API_VERIFY_SSL` has always defaulted to `false`; the README claimed
  `true`. The default becomes `true` in 2.0.0 — set the variable explicitly to keep today's
  behaviour. All samples in `contrib/` already do.
- Boolean environment variables are validated: an unparseable value is a configuration error
  rather than a silent `false`.
- `setup.py` and `setup.cfg` removed — `pyproject.toml` is the only source of packaging metadata.
  Development tools moved to `pip install -e '.[dev]'`; `requirements.txt` now pins runtime
  dependencies only.

### Known issue

- **`DRY_RUN` is only partial.** It suppresses provisioning, node status updates and cleanup, but
  VM, interface, IP and disk records are still written to NetBox. Documented in the README; a fix
  is planned for a following release.

## [1.0.7] - 2026-05-10

### Added

- **`PRIMARY_SUBNETS`** — optional, comma- or whitespace-separated list of subnets used to pick `primary_ip4` / `primary_ip6` for each VM (e.g. `PRIMARY_SUBNETS=192.168.88.0/24, 2001:db8::/64`). Subnet order defines priority: for IPv4 and for IPv6 independently, the first subnet in the list that matches an IP reported by the QEMU guest agent wins. Within a subnet candidate IPs are sorted, so the result is deterministic regardless of the order Proxmox/agent returns interfaces. When the variable is unset/empty, pve2netbox does **not** touch `primary_ip4`/`primary_ip6` (see 1.0.7 below). Invalid subnet are logged and skipped.

## [1.0.6] - 2026-04-24

### Fixed

- **Backup-induced changelog noise**: during `vzdump` of a stopped VM, Proxmox briefly reports `status: running` (a helper QEMU is started for disk access), which previously produced `offline → active → offline` entries in the NetBox changelog on every backup. Status is now preserved unchanged while PVE holds a transient `lock` (`backup`, `snapshot`, `migrate`, `clone`, `rollback`); the same condition also prevents quick-check from flagging the VM as "changed". Controlled by new env var **`IGNORE_STATUS_WHEN_LOCKED`** (default `true`).

## [1.0.5] - 2026-04-16

### Fixed

- **Stale VM disks cleanup**: when a disk is moved to another Proxmox storage, its path (NetBox disk `name`) changes and a new entry was previously added without removing the old one. The sync now tracks disks seen in the current Proxmox config and deletes obsolete `VirtualDisk` records from NetBox for both QEMU VMs and LXC containers.

### Changed

- **LXC installer auto-restart on update**: `contrib/lxc/install.sh` now detects already-running `pve2netbox.service` / `pve2netbox.timer` units and restarts them automatically after re-running the installer, so in-place upgrades pick up the new code without manual `systemctl restart`.

## [1.0.4] - 2026-03-21

### Added

- **Proxmox tag sync to NetBox** — native VM/LXC tags from Proxmox (semicolon-separated in cluster resources) are created in NetBox when missing and assigned to the matching virtual machines together with existing pool tags (`Pool/<poolid>`). New env var **SYNC_TAGS** (default `true`); set `SYNC_TAGS=false` to keep only pool tags. Documented in README, `.env.example`, and `contrib/` samples (Docker, LXC, systemd).

## [1.0.3] - 2026-02-18

### Fixed

- **QEMU guest agent**: when syncing VM interfaces, IP addresses no longer reported by the guest agent are now removed from the interface in NetBox (previously only new IPs were added; stale ones were left).

## [1.0.2] - 2026-02-17

### Fixed

- VM matching by name+cluster when vmid is missing; sync no longer aborts on single VM/LXC failure; in combined mode, failed initial full sync triggers full retry on next cycle.

### Added

- VM index by `(name, cluster_id)` and per-VM/LXC error handling with clear messages.


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
