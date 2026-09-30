# The official Core checker and its integration dependencies must match runtime evidence.
FROM ghcr.io/home-assistant/home-assistant:2026.9.4@sha256:3e6710a7ab2a61311d9d899b719f6c3657791c63e8f4942cec4ebc42401d6b76

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
COPY github_known_hosts /app/github_known_hosts
# Keep SyncApp dependencies separate from the official Core Python environment. The
# validator gets a distinct executable path so HA OS AppArmor can force a child-profile
# transition before any candidate/configuration bytes are read.
RUN apk add --no-cache git openssh-client libseccomp \
    && python3 -m venv /opt/syncapp \
    && /opt/syncapp/bin/python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt \
    && install -d -m 0555 /opt/syncapp-validator \
    && install -m 0555 /usr/local/bin/python3 /opt/syncapp-validator/python3
COPY src/ /app/

STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/syncapp/bin/python", "-m", "ha_syncapp.launcher"]
CMD []
