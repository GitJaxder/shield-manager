"""Constants for the Shield Manager integration."""

from datetime import timedelta

DOMAIN = "shield_manager"

# The Shield Manager app's port inside Home Assistant's network.
DEFAULT_PORT = 8765

# How often to ask the app for running jobs and online updates (both cheap), and how
# often to re-read every Shield's app list (this connects to each Shield).
UPDATE_INTERVAL = timedelta(seconds=30)
CATALOG_INTERVAL = timedelta(minutes=5)

ATTR_DEVICES = "devices"
ATTR_PACKAGES = "packages"
ATTR_ALLOW_DOWNGRADE = "allow_downgrade"
ATTR_DOWNLOADS = "downloads"

CONF_URL = "url"
# The app's full slug (repository hash + slug), to link to its sidebar page.
CONF_APP_SLUG = "app_slug"
