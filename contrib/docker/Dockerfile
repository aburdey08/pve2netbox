FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY pve2netbox/ pve2netbox/
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python", "-m", "pve2netbox"]
