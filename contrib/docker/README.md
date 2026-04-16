# Docker: pve2netbox

Run pve2netbox in a Docker container via `docker compose`. One compose file per mode — just edit the `environment` block and start.

---

## Quick start — Combined mode (recommended)

Quick change-check every minute + full sync every hour. Best balance for most setups.

```bash
git clone https://github.com/aburdey08/pve2netbox.git
cd pve2netbox

# 1. Edit credentials in the compose file (PVE_API_*, NB_API_*):
$EDITOR contrib/docker/docker-compose.combined-mode.yml

# 2. Start:
docker compose -f contrib/docker/docker-compose.combined-mode.yml up -d

# 3. Watch logs:
docker compose -f contrib/docker/docker-compose.combined-mode.yml logs -f
```

All commands are run **from the repository root**.

---

## Other modes

| Mode | Compose file | Start command |
|------|--------------|---------------|
| **Single-run** — one sync, then exit | `docker-compose.single-run.yml` | `docker compose -f contrib/docker/docker-compose.single-run.yml run --rm pve2netbox` |
| **Simple** — full sync every N seconds | `docker-compose.simple-mode.yml` | `docker compose -f contrib/docker/docker-compose.simple-mode.yml up -d` |
| **Combined** — quick check + full sync | `docker-compose.combined-mode.yml` | `docker compose -f contrib/docker/docker-compose.combined-mode.yml up -d` |

Mode is selected by the interval variables in the compose file — see [main README — Operation modes](../../README.md#operation-modes).

---

## Configuration

Everything is set in the `environment` block of the compose file you're running.

**Required:** `PVE_API_HOST`, `PVE_API_USER`, `PVE_API_TOKEN`, `PVE_API_SECRET`, `NB_API_URL`, `NB_API_TOKEN`.

Full variable reference: [main README — Configuration](../../README.md#configuration) and [.env.example](../../.env.example).

---

## Check

```bash
# Logs:
docker compose -f contrib/docker/docker-compose.<mode>.yml logs -f pve2netbox

# Prometheus metrics (if ENABLE_METRICS=true):
curl http://localhost:9090/metrics
```
