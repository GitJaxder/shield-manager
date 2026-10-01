#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Devices, settings, the ADB key and the app-icon cache live in /data, which Home
# Assistant keeps across restarts, updates and backups.
export SHIELD_MANAGER_HOME=/data
PORT=8765

# A key pasted in the options replaces the app's own, so Shields that already trust it
# (for example Home Assistant's Android TV integration) don't ask again. Without one, the
# app makes its own key on first use.
if bashio::config.has_value 'adb_key'; then
    if bashio::config 'adb_key' | shield-manager key import >/dev/null; then
        bashio::log.info "Using the ADB key from the app's options"
    else
        bashio::log.warning "The ADB key in the app's options isn't a valid private key; ignoring it"
    fi
fi

# 172.30.32.2 is Home Assistant's ingress proxy (the sidebar page). Nothing else may
# connect, because the page has no login of its own.
bashio::log.info "Starting Shield Manager"
exec shield-manager web --host 0.0.0.0 --port "${PORT}" \
    --allow-from 172.30.32.2
