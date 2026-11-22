FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FACTURADOR_CONTAINER=1 \
    FACTURADOR_DATA=/data \
    FACTURADOR_CHROMIUM_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium libatomic1 fonts-liberation fonts-noto-color-emoji \
        tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 facturador \
    && install -d -m 0700 -o facturador -g facturador /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright --version
COPY facturador ./facturador
COPY apps/api/src/vision/receipt_scan.py ./apps/api/src/vision/receipt_scan.py
COPY web ./web
COPY PROMPT_FACTURADOR.md ./

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/healthz', timeout=2).close()"]
CMD ["python", "-m", "facturador"]
