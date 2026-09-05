# pve2netbox

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-0db7ed?logo=docker&logoColor=white)](https://hub.docker.com/r/aburdey/pve2netbox)
[![tests](https://github.com/aburdey08/pve2netbox/actions/workflows/tests.yml/badge.svg)](https://github.com/aburdey08/pve2netbox/actions/workflows/tests.yml)

Sync Proxmox VE (PVE) inventory to NetBox: QEMU VMs and LXC containers, their disks, network interfaces, IP addresses and tags.

Based on [creekorful/netbox-pve-sync](https://github.com/creekorful/netbox-pve-sync).

---

## Quick start

Pick **one** of the two recommended options. Both use **Combined mode** — quick change-check every minute, full sync every hour (best balance for most setups).

Before you start, you need:

- A **NetBox** API token with write access and a target cluster (`NB_CLUSTER_ID` or `NB_CLUSTER_NAME`).
- A **Proxmox VE** user + API token with `Pool.Audit`, `VM.Audit`, `Sys.Audit`.
- Physical nodes already created in NetBox with names **matching** Proxmox node names.

### Option A — Docker Compose (recommended)

```bash
git clone https://github.com/aburdey08/pve2netbox.git
cd pve2netbox

# 1. Edit credentials (PVE_API_*, NB_API_*) in the compose file:
nano contrib/docker/docker-compose.combined-mode.yml

# 2. Start:
docker compose -f contrib/docker/docker-compose.combined-mode.yml up -d

# 3. Watch logs:
docker compose -f contrib/docker/docker-compose.combined-mode.yml logs -f
```

That's it. More details and other modes: [contrib/docker/](contrib/docker/).

### Option B — LXC on Proxmox (one command)

Run **on a Proxmox node as root** — creates a Debian 12 LXC and installs everything. If a **`.env`** file is placed next to the script, it is copied into the container as `/etc/pve2netbox/env` automatically — the service is fully configured right after deploy.

```bash
# Testing a specific branch instead of master? export REPO_BRANCH=your-branch first.
export REPO_BRANCH="${REPO_BRANCH:-master}"

# 1. Get the Combined-mode sample and the deploy script:
curl -sL "https://raw.githubusercontent.com/aburdey08/pve2netbox/$REPO_BRANCH/contrib/lxc/env.combined-mode" -o .env
curl -sL "https://raw.githubusercontent.com/aburdey08/pve2netbox/$REPO_BRANCH/contrib/lxc/deploy-from-pve.sh" -o deploy-from-pve.sh
chmod +x deploy-from-pve.sh

# 2. Fill in PVE_API_* and NB_API_* in .env:
nano .env

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
| `NB_CLUSTER_ID` **or** `NB_CLUSTER_NAME` | Target NetBox cluster — an existing ID, or a name that is created if missing |

Any existing cluster ID works — nothing assumes `1`. `NB_CLUSTER_ID` must point at a cluster that
already exists; `NB_CLUSTER_NAME` is created (together with the `Proxmox VE` cluster type) when
missing. Setting both is allowed only when they refer to the same cluster; a mismatch is reported as
a configuration error rather than silently picking one.

Common optional variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `PVE_API_VERIFY_SSL` | `false` | Verify PVE SSL cert. **Changes to `true` in 2.0.0** — set it explicitly now |
| `VM_ROLE` / `LXC_ROLE` | — | NetBox device role for VMs / LXC (created if missing) |
| `SYNC_VMS` / `SYNC_LXC` / `SYNC_TAGS` | `true` | Enable/disable each sync type |
| `NODE_MISSING_POLICY` | `skip` | Proxmox node with no matching NetBox device: `skip` (log and continue) or `fail` (stop the run) |
| `DRY_RUN` | `false` | **Partial** — see [Dry-run limitations](#dry-run-limitations) below |
| `ENABLE_CLEANUP` | `false` | Delete from NetBox VMs missing in PVE (**use with care**) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENABLE_METRICS` / `METRICS_PORT` | `false` / `9090` | Prometheus metrics on `/metrics` |
| `ENABLE_HEALTH_ENDPOINT` | `true` | `/healthz` and `/readyz` on `METRICS_PORT` |
| `PRIMARY_SUBNETS` | — | Comma/space-separated subnets used to pick `primary_ip4`/`primary_ip6` (first matching subnet wins, IPv4/IPv6 independent). Empty = leave `primary_ip*` untouched. Example: `192.168.88.0/24, 2001:db8::/64` |
| `LXC_IP_SOURCE` | `auto` | Where container IPs come from: `auto` (running container, falling back to static config), `runtime`, `config`, `none` |
| `SYNC_DESCRIPTION` | `true` | Copy the PVE description (Notes) into NetBox |
| `DESCRIPTION_TARGET` | `comments` | Target field: `comments` (multi-line) or `description` (200 chars) |
| `SYNC_PLATFORM` | `false` | Map `ostype` to a NetBox platform (created if missing) |
| `PLATFORM_MAP` | — | Override the built-in mapping: `l26=Linux,win11=Windows 11` |
| `POOL_AS_TENANT` | `false` | Also map the Proxmox pool to a NetBox tenant (created if missing) |
| `TEMPLATE_POLICY` | `tag` | Templates: `tag` (sync + tag `pve-template`), `skip`, `sync` |
| `NB_PRELOAD_SCOPE` | `cluster` | How much of NetBox is read before a sync — see [Tuning NetBox load](#tuning-netbox-load) |

Selecting what gets synced:

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_NODES` | — | Only these Proxmox nodes are synced |
| `EXCLUDE_NODES` | — | These nodes are skipped |
| `SYNC_POOLS` | — | Only guests in these Proxmox pools |
| `INCLUDE_TAGS` | — | Only guests carrying at least one of these PVE tags |
| `EXCLUDE_TAGS` | — | Guests carrying any of these PVE tags are skipped |
| `EXCLUDE_VMIDS` | — | Single IDs and ranges: `100,105,900-999` |

Full list and comments: [.env.example](.env.example).

---

## Selecting what to sync

Lists are comma- or space-separated and matched case-insensitively; PVE tags are matched whole
(`db` does not match `dbserver`). A guest has to pass every configured filter, and the first rule
that rejects it is what gets logged at `DEBUG`:

```
SYNC_NODES=pve1,pve2          # ignore the rest of the cluster
EXCLUDE_TAGS=no-netbox        # opt individual guests out from the PVE UI
EXCLUDE_VMIDS=900-999         # keep a scratch ID range out of the inventory
```

Every sync logs one summary line — `Filters excluded 12 of 340 guest(s) (EXCLUDE_TAGS: 8,
EXCLUDE_VMIDS: 4)` — and the same number is exported as `pve2netbox_guests_filtered`.

**Filtered guests are never deleted.** They still exist in Proxmox, so `ENABLE_CLEANUP=true`
leaves their NetBox records untouched; only guests that are really gone are removed. The same now
holds for `SYNC_VMS=false` / `SYNC_LXC=false`, which used to make cleanup delete every VM or
container of the disabled type, and for the guests of a node skipped by
`NODE_MISSING_POLICY=skip` — a node missing its NetBox device goes unsynced, but its guests are
not deleted.

Filters apply to the full sync, the quick check and cleanup alike. `SYNC_POOLS` is the one
exception: pool membership needs `/cluster/resources`, the only endpoint that answers it in a
single request. Where the API token cannot read it, the full sync rebuilds pool membership from
`/pools` — a separate permission — and carries on; the quick check does not, so a pool move is
then picked up by the next full sync rather than within the quick-check interval. Only when
neither endpoint is readable does the sync stop with a configuration error instead of quietly
matching no guest.

Reading pools matters beyond `SYNC_POOLS`: tags are written to NetBox wholesale, so a pass that
cannot see pools would strip the `Pool/*` tag off every guest it touches. If that ever happens
you get a warning naming the consequence, and the tag comes back on the next pass that can read
pools.

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
- **pip3 from source** — `pip install .` in repo root, then run `pve2netbox`. Configuration comes
  from the environment, from `--env-file PATH`, from `$PVE2NETBOX_ENV_FILE`, or from a `.env` file in
  the current directory. Variables already set in the environment take precedence over the file.
  `pve2netbox --help` lists the options. Requires Python 3.9+

---

## How it works

Hits the Proxmox VE API, reads VMs/LXC, and creates/updates NetBox objects accordingly.

**Supported:**

- **QEMU VMs** — disks (SCSI/SATA/VirtIO/IDE/EFI), NICs with VLAN and MTU, IPs via QEMU Guest Agent (interfaces matched by MAC).
- **LXC containers** — rootfs and mount points (`mp0`, `mp1`…), NICs with MTU, and IP addresses (see below).
- **Both** — Proxmox notes, `ostype` as platform, pool as tenant, and template marking (all except notes are opt-in).

**QEMU Guest Agent** (when `agent=1` and VM is running): real OS interface names (e.g. `eth0`) instead of `net0`, MAC-based matching, IPv4/IPv6 assignment to NetBox interfaces.

**LXC IP addresses** (since 1.1.0) come from two sources, selected with `LXC_IP_SOURCE`:

| Source | Where from | Works for |
|--------|-----------|-----------|
| runtime | `GET /nodes/{node}/lxc/{vmid}/interfaces` | Running containers, **including DHCP** |
| config | `ip=` / `ip6=` in `net0`, `net1`… | Static addresses, container running or not |

`auto` (default) prefers runtime and falls back to the static config — per interface, so a container
that reports only some of its NICs still gets the rest from its config. When the endpoint is
unavailable (older PVE, token without `VM.Audit`) the run logs a warning and uses the static
addresses instead of failing. Loopback and link-local (`fe80::`) addresses are never written to
NetBox. Because the data reaches NetBox through the same path as guest-agent data,
`PRIMARY_SUBNETS` sets `primary_ip4`/`primary_ip6` for containers too.

**Auto-created in NetBox on first run:**

| Object | Name | Type | Scope |
|--------|------|------|-------|
| Custom field | `autostart` | Boolean | Virtual Machine |
| Custom field | `replicated` | Boolean | Virtual Machine |
| Custom field | `ha` | Boolean | Virtual Machine |
| Custom field | `backup` | Boolean | Virtual Disk |
| Custom field | `dns_name` | Text | Prefix |
| Device role | from `VM_ROLE` / `LXC_ROLE` | — | `vm_role=true` |
| Tag | `pve-template` | — | `TEMPLATE_POLICY=tag` |
| Platform | from `ostype` | — | `SYNC_PLATFORM=true` |
| Tenant | from the Proxmox pool name | — | `POOL_AS_TENANT=true` |

---

## Prometheus metrics and health endpoints

One HTTP server on `METRICS_PORT` (default `9090`) serves both:

| Endpoint | Enabled by | Meaning |
|----------|-----------|---------|
| `/metrics` | `ENABLE_METRICS=true` | Prometheus exposition format |
| `/healthz` | `ENABLE_HEALTH_ENDPOINT=true` (default) | Liveness — 200 while the process runs |
| `/readyz` | `ENABLE_HEALTH_ENDPOINT=true` (default) | Readiness — 503 when no sync has succeeded yet, or the last successful full sync is older than two sync intervals |

The Docker image's `HEALTHCHECK` uses `/readyz`, so a container whose syncs are failing is reported
as `unhealthy` instead of merely logging errors.

Metrics: `pve2netbox_build_info`, `pve2netbox_full_syncs_total`, `pve2netbox_quick_checks_total`,
`pve2netbox_vms_synced_total`, `pve2netbox_lxc_synced_total`, `pve2netbox_errors_total`,
`pve2netbox_vms_tracked`, `pve2netbox_lxc_tracked`, `pve2netbox_last_sync_duration_seconds`,
`pve2netbox_last_sync_timestamp_seconds` (last attempt), `pve2netbox_last_success_timestamp_seconds`
(last success — alert on this one), `pve2netbox_changes_detected`,
`pve2netbox_guests_filtered` (guests excluded by the selection filters in the last full sync).

## Dry-run limitations

`DRY_RUN=true` currently suppresses only custom field and role provisioning, node status updates and
cleanup. **VM, interface, IP and disk records are still written to NetBox.** Use a test NetBox
instance until this is fixed.

## Shutdown behaviour

SIGTERM and SIGINT are handled cooperatively: the current object finishes syncing, the run stops at
the next node or VM boundary, and the process exits with code 0. `docker stop` and
`systemctl restart` no longer interrupt a write half-way. A partial run never triggers
`ENABLE_CLEANUP` deletions.

---

## Tuning NetBox load

If NetBox returns 502s under load:

- `NB_API_DELAY_SECONDS` — delay between requests (default `0.2`; try `0.5`–`1.0`).
- `NB_API_RETRY_TOTAL` — retries on 502/503/429 (default `5`).
- `NB_API_RETRY_BACKOFF` — backoff factor (default `1.0`).

`NB_PRELOAD_SCOPE` controls how much of NetBox is read into memory before each sync:

| Value | Behaviour |
|-------|-----------|
| `cluster` (default) | Virtual machines, their interfaces and their virtual disks are fetched with a `cluster_id` filter, and only devices named like a Proxmox node are read |
| `all` | Every device, VM, interface and disk in NetBox — the behaviour before 1.2.0 |

IP addresses, prefixes, MAC addresses, VLANs, tags and roles are always read globally: they are
matched and de-conflicted across the whole of NetBox, and scoping them would change what the sync
writes. Each sync logs what it loaded (`NetBox objects loaded: 3 devices, 210 VMs, …`).

Use `all` only if VMs have to be adopted into this cluster from another one. It is a performance
setting and nothing more: `ENABLE_CLEANUP` checks each VM's cluster before deleting it, so VMs
belonging to another cluster are safe under either value.

How much it saves depends on how much of NetBox belongs to other clusters — the global IPAM read
is the floor both values pay. Measured with `tools/preload_bench.py` on a NetBox of 10 clusters,
one of them synced:

| Inventory | Scope | Requests | Records | Read | Time | Peak memory |
|-----------|-------|---------:|--------:|-----:|-----:|------------:|
| 3 000 VMs, 20 000 IPs | `cluster` | 35 | 29 810 | 16.3 MiB | 13.0 s | 114 MiB |
| | `all` | 47 | 43 800 | 24.8 MiB | 20.4 s | 190 MiB |
| 5 000 VMs, 50 000 IPs | `cluster` | 76 | 71 812 | 39.1 MiB | 34.1 s | 272 MiB |
| | `all` | 98 | 95 100 | 53.4 MiB | 45.3 s | 398 MiB |

Below that scale there is nothing to save: on a real NetBox of 36 VMs and 70 IP addresses both
values read the same 314 records, and `cluster` spent two requests more widening a device query.
It never costs more than that.

Measure your own installation:

```bash
python tools/preload_bench.py                    # a generated inventory, no NetBox needed
python tools/preload_bench.py --mode live        # the NetBox from your .env, read-only
```

---

## Development

```bash
pip install -e '.[dev]'
pytest          # filter rules, config parsing, and the guards around NetBox reads
pylint pve2netbox
```

The tests use no live Proxmox or NetBox. They pin the filter verdicts — the full sync, the quick
check and cleanup have to agree about every guest — the behaviour of a NetBox read that fails or
answers something other than what was asked for, and the promise that `NB_PRELOAD_SCOPE` changes
only how much is read, never what the sync is given.

`tools/preload_bench.py` measures that preload; it is a development tool and not part of the
installed package.
