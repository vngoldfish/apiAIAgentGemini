FROM python:3.11-slim-bullseye

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Safer defaults for long-running gateway
    SESSION_MAX_COUNT=500 \
    SESSION_TTL_SECONDS=3600 \
    MAX_FAILOVER_ATTEMPTS=3 \
    WATCHDOG_INTERVAL_SECONDS=45 \
    PROACTIVE_COOKIE_ON_EVERY_REQUEST=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4 \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    curl-cffi~=0.15.0 \
    loguru~=0.7.3 \
    orjson~=3.11.7 \
    pydantic~=2.12.5 \
    fastapi \
    uvicorn \
    httpx \
    imageio-ffmpeg>=0.5.1

COPY src /app/src
COPY dashboard_server.py dashboard.html index.html playground.html api_server.py media_services.py media_pipeline.py /app/

# Persist cookie auto-refresh files if GEMINI_COOKIE_PATH is used
RUN mkdir -p /app/static /app/gemini_cookies \
    && touch /app/api_keys.json /app/gemini_accounts.json /app/gemini_agents.json /app/dashboard_config.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" || exit 1

# Single worker — sticky sessions + in-memory pool are not multi-worker safe
CMD ["python", "-c", "import uvicorn; uvicorn.run('api_server:app', host='0.0.0.0', port=8000, workers=1, log_level='info', access_log=True, timeout_keep_alive=75)"]
