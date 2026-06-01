# Reliquary MCP memory server.
FROM python:3.12-slim

# Avoid .pyc + buffered stdout in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code. Running `app/server.py` puts app/ on sys.path[0], so the flat
# `from oauth import ...` imports resolve.
COPY app/ ./app/

EXPOSE 8787

# MCP-only by default (no chat upstream). Config + secrets are provided at run
# time (mounted config.yaml + env vars). Bind to 0.0.0.0 inside the container;
# publish it only behind a reverse proxy / tunnel that terminates TLS.
CMD ["python", "app/server.py", \
     "--config", "/config/config.yaml", \
     "--host", "0.0.0.0", "--port", "8787", \
     "--no-chat-upstream"]
