FROM python:3.14.7-slim-bookworm

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="Home Assistant SyncApp V2" \
      org.opencontainers.image.source="https://github.com/Togarriapa/HomeAssistant_SyncAppV2"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/ /app/

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "ha_syncapp"]
