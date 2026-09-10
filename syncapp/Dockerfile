# The official Core checker and its integration dependencies must match runtime evidence.
FROM ghcr.io/home-assistant/home-assistant:2026.9.1@sha256:612d76760b544cb40b7ba01387fdac964c59a6a550a50a4d30b4773c822d2918

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
# Keep SyncApp dependencies separate from the official Core Python environment.
RUN apk add --no-cache git openssh-client libseccomp \
    && python3 -m venv /opt/syncapp \
    && /opt/syncapp/bin/python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY src/ /app/

STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/syncapp/bin/python", "-m", "ha_syncapp"]
CMD []
