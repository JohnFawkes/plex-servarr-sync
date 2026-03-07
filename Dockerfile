# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN groupadd --gid 1000 appuser \
 && useradd  --uid 1000 --gid 1000 --no-create-home appuser

WORKDIR /app

# ── Python deps (cached layer) ────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip>=26.0 \
    && pip install --no-cache-dir -r requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
COPY media-servarr-sync.py .
COPY templates/ templates/

# Create data directory for persistent storage
RUN mkdir -p /data && chown appuser:appuser /data

# Drop privileges
USER appuser

# ── Runtime ───────────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ARG PORT=5000
ENV PORT=${PORT}
EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD /bin/sh -c 'curl -sf http://localhost:${PORT:-5000}/health || exit 1'

ENTRYPOINT ["python", "media-servarr-sync.py"]
