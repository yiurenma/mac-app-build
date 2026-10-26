# Headless Linux image: Web Console + scheduler. Build/run notes: docs/29-Linux与Docker部署.md
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git xvfb && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt keyring
COPY . .
ENV PYTHONUNBUFFERED=1
VOLUME ["/app/.appbuilder"]
# Secrets come from APPBUILDER_SECRET_* env vars or *_FILE (Docker secrets), never the image.
CMD ["sh", "-c", "python3 -m appbuilder scheduler run & exec xvfb-run -a python3 -m appbuilder web --no-open"]
