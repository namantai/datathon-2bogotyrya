FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENVIRONMENT=production \
    APP_DEBUG=false \
    APP_ANALYZER_MODE=disabled \
    TMPDIR=/var/lib/forteguard/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 forteguard \
    && useradd --uid 10001 --gid forteguard --no-create-home forteguard \
    && mkdir -p /var/lib/forteguard/uploads /var/lib/forteguard/tmp \
    && chown -R forteguard:forteguard /var/lib/forteguard \
    && chmod 700 /var/lib/forteguard /var/lib/forteguard/uploads /var/lib/forteguard/tmp

WORKDIR /app
# Never COPY the repository, local .env, storage, tests or ML assets.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/src/ ./src/
RUN python -m pip install --no-cache-dir . && python -m pip check

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["uvicorn", "fortebank_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "16", "--timeout-graceful-shutdown", "20"]
