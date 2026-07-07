# syntax=docker/dockerfile:1

# ── Stage 1: build Python wheels ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libjpeg62-turbo-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --prefix=/install \
    "discord.py>=2.3.2" \
    "fastapi>=0.110.0" \
    "uvicorn[standard]>=0.27.0" \
    "qrcode[pil]>=7.4.2" \
    "pycryptodome>=3.20.0" \
    "httpx>=0.27.0" \
    "websockets>=12.0" \
    "python-dotenv>=1.0.0" \
    "aiohttp>=3.9.0" \
    "cryptography>=42.0.0"

# ── Stage 2: minimal runtime ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QR_SERVER_HOST=0.0.0.0 \
    QR_SERVER_PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        dumb-init \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /install /usr/local

WORKDIR /app

COPY --chown=app:app experiment ./experiment
COPY --chown=app:app config.py ./
COPY --chown=app:app research ./research
COPY --chown=app:app analysis ./analysis
COPY --chown=app:app interface ./interface
COPY --chown=app:app server ./server
COPY --chown=app:app web ./web

RUN mkdir -p /app/output \
    && chown -R app:app /app

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"

ENTRYPOINT ["dumb-init", "--"]
CMD ["python", "-m", "experiment.main"]
