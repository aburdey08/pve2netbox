FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
COPY pve2netbox/ pve2netbox/
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

# Reports unhealthy when the last successful full sync is older than two sync
# intervals, or when no sync has succeeded yet. Requires the health endpoint
# (ENABLE_HEALTH_ENDPOINT=true, the default) on METRICS_PORT.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.getenv('METRICS_PORT', '9090') + '/readyz', timeout=5).status == 200 else 1)"

STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "-m", "pve2netbox"]
