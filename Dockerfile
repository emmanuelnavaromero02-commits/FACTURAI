ARG TARGETARCH=amd64
ARG FACTURAI_BROWSER=browser-${TARGETARCH}
FROM python:3.12-slim-bookworm AS browser-chromium

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTURADOR_CONTAINER=1 \
    FACTURADOR_DATA=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/facturador-browsers \
    FACTURADOR_CHROMIUM_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium libatomic1 fonts-liberation fonts-noto-color-emoji \
        tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 facturador \
    && install -d -m 0700 -o facturador -g facturador /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps firefox \
    && rm -rf /var/lib/apt/lists/*
CMD ["python", "-m", "facturador"]

FROM browser-chromium AS browser-arm64

FROM browser-chromium AS browser-amd64
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gpg gpg-agent xauth xvfb; \
    key_home="$(mktemp -d)"; \
    python -c "from urllib.request import urlretrieve; urlretrieve('https://dl.google.com/linux/linux_signing_key.pub', '/tmp/google-linux-key.pub')"; \
    gpg --batch --homedir "$key_home" --import /tmp/google-linux-key.pub; \
    fingerprint=EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796; \
    install -d -m 0755 /etc/apt/keyrings; \
    gpg --batch --homedir "$key_home" --export "$fingerprint" > /etc/apt/keyrings/google-chrome.gpg; \
    test -s /etc/apt/keyrings/google-chrome.gpg; \
    chmod 0644 /etc/apt/keyrings/google-chrome.gpg; \
    printf '%s\n' 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list; \
    printf '%s\n' 'repo_add_once="false"' 'repo_reenable_on_distupgrade="false"' > /etc/default/google-chrome; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable; \
    google-chrome-stable --version; \
    rm -rf "$key_home" /tmp/google-linux-key.pub /var/lib/apt/lists/*
ENV FACTURADOR_CHROMIUM_PATH=/usr/bin/google-chrome-stable \
    FACTURADOR_CHROME_HEADED=1
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1366x900x24 -nolisten tcp", "python", "-m", "facturador"]

FROM ${FACTURAI_BROWSER} AS facturai
COPY facturador ./facturador
COPY apps/api/src/vision/receipt_scan.py ./apps/api/src/vision/receipt_scan.py
COPY web ./web
COPY PROMPT_FACTURADOR.md ./

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import json; from urllib.request import urlopen; assert json.load(urlopen('http://127.0.0.1:8765/healthz', timeout=2)).get('status') == 'ok'"]
