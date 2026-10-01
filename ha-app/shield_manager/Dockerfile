FROM ghcr.io/home-assistant/base-python:3.13-alpine3.23

# The app's version (from config.yaml), set by Home Assistant when it builds the app.
ARG BUILD_VERSION
# Which tag, branch or commit of shield-manager to install: by default the release tag
# matching the app's version, so every install of a version gets the same code.
ARG SHIELD_MANAGER_REF=v${BUILD_VERSION}

RUN pip install --no-cache-dir \
        "https://github.com/GitJaxder/shield-manager/archive/${SHIELD_MANAGER_REF}.tar.gz" \
    && echo "Installed shield-manager for app version ${BUILD_VERSION}"

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="aarch64|amd64" \
    org.opencontainers.image.title="Shield Manager" \
    org.opencontainers.image.source="https://github.com/GitJaxder/shield-manager" \
    org.opencontainers.image.licenses="GPL-3.0-or-later"
