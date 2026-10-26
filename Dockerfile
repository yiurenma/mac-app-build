# Headless Linux image: Web Console + scheduler. Build/run notes: docs/29-Linux与Docker部署.md
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git xvfb && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt keyring
COPY . .
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/.appbuilder"]
# Secrets come from APPBUILDER_SECRET_* env vars or *_FILE (Docker secrets), never the image.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
