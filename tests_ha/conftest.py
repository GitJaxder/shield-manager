"""Tests for the Home Assistant integration in custom_components/shield_manager.

They need Home Assistant's test harness, which needs a newer Python than shield-manager
itself, so CI runs them in their own job:

    pip install -e . pytest-homeassistant-custom-component
    pytest tests_ha -o asyncio_mode=auto
"""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
