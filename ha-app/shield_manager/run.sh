#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Devices, settings, the ADB key and the app-icon cache live in /data, which Home
# Assistant keeps across restarts, updates and backups.
export SHIELD_MANAGER_HOME=/data
PORT=8765
ANDROID_TV_KEY=/homeassistant/.storage/androidtv_adbkey

if [ ! -f /data/adbkey ] && bashio::config.true 'use_android_tv_key'; then
    if [ -f "${ANDROID_TV_KEY}" ] && [ -f "${ANDROID_TV_KEY}.pub" ]; then
        cp "${ANDROID_TV_KEY}" /data/adbkey
        cp "${ANDROID_TV_KEY}.pub" /data/adbkey.pub
        chmod 600 /data/adbkey
        bashio::log.info "Using the Android TV integration's ADB key"
    else
        bashio::log.info "No Android TV integration key found; making a new key"
    fi
fi

# Announce this app to the Shield Manager integration. The container's hostname is the
# address other apps and Home Assistant reach it on.
if ! bashio::discovery "shield_manager" \
    "$(bashio::var.json host "$(hostname)" port "^${PORT}")" >/dev/null; then
    bashio::log.warning "Couldn't announce the app to the Shield Manager integration"
fi

# 172.30.32.2 is Home Assistant's ingress proxy (the sidebar page). 172.30.32.1 is Home
# Assistant itself, for the integration. Nothing else may connect, because the page has
# no login of its own.
bashio::log.info "Starting Shield Manager"
exec shield-manager web --host 0.0.0.0 --port "${PORT}" \
    --allow-from 172.30.32.2 --allow-from 172.30.32.1
