FROM ghcr.io/home-assistant/base-python:3.13-alpine3.23

# The app's version (from config.yaml). Using it here makes a new version download the
# source again instead of reusing the cached install.
ARG BUILD_VERSION
# Which branch, tag or commit of shield-manager to install.
ARG SHIELD_MANAGER_REF=main

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
