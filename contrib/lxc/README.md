# LXC: pve2netbox

Run pve2netbox inside an LXC container on Proxmox VE. Inside the container the app is managed by systemd ([contrib/systemd/](../systemd/)).

---

## Quick start — one command from a Proxmox node

The **`deploy-from-pve.sh`** script creates a Debian 12 LXC, starts it, and installs pve2netbox.

Run **on a Proxmox VE node as root**:

```bash
# Testing a specific branch instead of master? export REPO_BRANCH=your-branch first.
export REPO_BRANCH="${REPO_BRANCH:-master}"

curl -sL "https://raw.githubusercontent.com/aburdey08/pve2netbox/$REPO_BRANCH/contrib/lxc/deploy-from-pve.sh" -o deploy-from-pve.sh
chmod +x deploy-from-pve.sh
./deploy-from-pve.sh
```

Interactively asks for: container ID, hostname, storage.

### Tip: pre-fill config with `.env` — no manual edit needed

If a **`.env`** file sits **next to** `deploy-from-pve.sh`, the script copies it into the container as `/etc/pve2netbox/env` automatically — so the service is fully configured right after deploy. Fallback search paths: `../` and `../../` (i.e. repo root).

Recommended flow:

```bash
export REPO_BRANCH="${REPO_BRANCH:-master}"   # set to test another branch

# 1. Get the mode sample (Combined — recommended) and the deploy script:
curl -sL "https://raw.githubusercontent.com/aburdey08/pve2netbox/$REPO_BRANCH/contrib/lxc/env.combined-mode" -o .env
curl -sL "https://raw.githubusercontent.com/aburdey08/pve2netbox/$REPO_BRANCH/contrib/lxc/deploy-from-pve.sh" -o deploy-from-pve.sh
chmod +x deploy-from-pve.sh

# 2. Fill in PVE_API_* and NB_API_* in .env:
nano .env

# 3. Deploy — .env is picked up automatically:
./deploy-from-pve.sh
```

Other mode samples: `env.single-run`, `env.simple-mode` (same directory).

### After deploy — enable the service

```bash
# If you used the .env trick above — config is ready, just start:
pct exec <CTID> -- systemctl enable --now pve2netbox

# Otherwise — edit first, then start:
pct exec <CTID> -- nano /etc/pve2netbox/env
pct exec <CTID> -- systemctl enable --now pve2netbox
```

Check:

```bash
pct exec <CTID> -- systemctl status pve2netbox
pct exec <CTID> -- journalctl -u pve2netbox -f
```

### Script parameters

Passed as environment variables: `CTID`, `STORAGE`, `BRIDGE`, `LXC_HOSTNAME`, `ROOTFS_SIZE`, `MEMORY`, `TEMPLATE_STORAGE`, `REPO_BRANCH`, `REPO_RAW`, `REPO_GIT`.

Defaults: `CTID` — first free starting at 200, `BRIDGE` — `vmbr0`, `HOSTNAME` — `lxc-pve2netbox`, `ROOTFS_SIZE` — 2 GiB, `MEMORY` — 256 MiB, `REPO_BRANCH` — `master`.

Examples:

```bash
CTID=201 BRIDGE=vmbr1 ./deploy-from-pve.sh
./deploy-from-pve.sh --no-install   # create LXC only, skip app install
REPO_BRANCH=my-feature-branch ./deploy-from-pve.sh   # test a specific branch
```

---

## Configuration

Config file inside the container: **`/etc/pve2netbox/env`** (format `KEY=value`, no `export`).

Mode samples in this directory:

- `env.combined-mode` — **recommended** (quick check + full sync)
- `env.simple-mode` — periodic full sync only
- `env.single-run` — one-shot

Variable reference: see [main README — Configuration](../../README.md#configuration) and [.env.example](../../.env.example).

Network: from inside the LXC, both `PVE_API_HOST` and `NB_API_URL` must be reachable.

---

## Install into an existing LXC

Enter the container with `pct enter <CTID>` (or `pct exec <CTID> -- ...`), then:

```bash
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash
# testing a branch: REPO_BRANCH=your-branch curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/your-branch/contrib/lxc/install.sh | bash
```

The script installs dependencies, the package, and systemd units. On first run it seeds `/etc/pve2netbox/env` from `.env.example` (existing file is kept untouched).

`install.sh` itself honors `REPO_BRANCH` (default `master`), `REPO_RAW`, and `REPO_GIT` — the `curl` URL above must already point at the branch you want, since it downloads the script before `install.sh` gets a chance to read the variable.

Edit config and enable:

```bash
nano /etc/pve2netbox/env
systemctl enable --now pve2netbox
```

One-liner from the PVE host:

```bash
pct exec <CTID> -- bash -c 'curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash'
```

## Update

Re-run the install script inside the container — it will **not** overwrite your existing `/etc/pve2netbox/env`:

```bash
curl -sL https://raw.githubusercontent.com/aburdey08/pve2netbox/master/contrib/lxc/install.sh | bash
systemctl restart pve2netbox
```

---

## Operation modes

Selected via variables in `/etc/pve2netbox/env`. Same three modes as everywhere else — see [main README — Operation modes](../../README.md#operation-modes).

Mode 1 (single-run): run `pve2netbox` manually or via cron. Modes 2 and 3: `systemctl enable --now pve2netbox`.
