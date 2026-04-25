# Systemd: pve2netbox

Unit files for running pve2netbox as a system service on a host or inside an LXC — without Docker.

> **Note:** if you're installing into an LXC, [contrib/lxc/install.sh](../lxc/install.sh) does all of this for you (installs deps + package + these units). This page is for bare-host installs or fully manual setups.

---

## Quick start

### 1. Install the package

From the repo root (Python 3.8+, pip):

```bash
sudo pip3 install .
```

Or install directly from GitHub:

```bash
sudo pip3 install git+https://github.com/aburdey08/pve2netbox.git
```

### 2. Create the config

Pick a mode sample from this directory (`env.combined-mode` — **recommended**) and drop it into `/etc/pve2netbox/env`:

```bash
sudo mkdir -p /etc/pve2netbox
sudo cp env.combined-mode /etc/pve2netbox/env   # or env.simple-mode / env.single-run
sudo chmod 600 /etc/pve2netbox/env
sudo nano /etc/pve2netbox/env                # fill in PVE_API_* and NB_API_*
```

Format: `KEY=value`, no `export`.

### 3. Install unit files and start

```bash
sudo cp pve2netbox.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pve2netbox
```

### 4. Check

```bash
sudo systemctl status pve2netbox
sudo journalctl -u pve2netbox -f
```

---

## Configuration

Variables live in `/etc/pve2netbox/env`.

**Required:** `PVE_API_HOST`, `PVE_API_USER`, `PVE_API_TOKEN`, `PVE_API_SECRET`, `NB_API_URL`, `NB_API_TOKEN`.

Full variable reference: [main README — Configuration](../../README.md#configuration) and [.env.example](../../.env.example).

Mode is selected by interval variables — see [main README — Operation modes](../../README.md#operation-modes).

---

## Run options

### Long-running service (Simple / Combined mode) — recommended

One process, keeps running; systemd restarts it on failure after 60s.

```bash
sudo systemctl enable --now pve2netbox
```

### On a timer (Single-run on schedule)

One-shot per trigger, timer fires every 5 minutes:

```bash
sudo cp pve2netbox-oneshot.service pve2netbox.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pve2netbox.timer
```

Change the interval by editing `OnUnitActiveSec=5min` in `pve2netbox.timer`.

### Manual / cron (Single-run)

```bash
pve2netbox
```

Loads env from `/etc/pve2netbox/env` is **not** automatic in this mode — either `export` the variables or use `set -a; source /etc/pve2netbox/env; set +a; pve2netbox`.
