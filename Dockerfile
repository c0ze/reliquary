# Reliquary MCP memory server.
FROM python:3.12-slim

# Avoid .pyc + buffered stdout in containers. Mem0 sends usage telemetry to
# PostHog unless MEM0_TELEMETRY is false; set MEM0_TELEMETRY=true to opt in.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEM0_TELEMETRY=false

WORKDIR /srv

# Install deps first for layer caching. Use the fully-resolved lock so the image
# is reproducible and the runtime can't drift from the API the code targets.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# App code. Running `app/server.py` puts app/ on sys.path[0], so the flat
# `from oauth import ...` imports resolve.
COPY app/ ./app/

# Run as a non-root user (least privilege). Give it a real, writable HOME —
# mem0 writes ~/.mem0 (telemetry / user id) at startup, which fails for a
# system user whose home defaults to /nonexistent.
#
# The ids are fixed at uid 100 / gid 101 (what earlier images were allocated):
# bind-mounted data directories must be owned by 100:101 on the host, e.g.
#   mkdir -p data/blobs data/compiled data/state && sudo chown -R 100:101 data
# The /data directories are created here too, so named volumes start writable.
RUN addgroup --system --gid 101 appuser \
    && adduser --system --uid 100 --ingroup appuser --home /home/appuser appuser \
    && mkdir -p /home/appuser /data/blobs /data/compiled /data/state \
    && chown -R appuser:appuser /srv /home/appuser /data
ENV HOME=/home/appuser
USER appuser

EXPOSE 8787

# MCP-only by default (no chat upstream). Config + secrets are provided at run
# time (mounted config.yaml + env vars). Bind to 0.0.0.0 inside the container;
# publish it only behind a reverse proxy / tunnel that terminates TLS.
CMD ["python", "app/server.py", \
     "--config", "/config/config.yaml", \
     "--host", "0.0.0.0", "--port", "8787", \
     "--no-chat-upstream"]
