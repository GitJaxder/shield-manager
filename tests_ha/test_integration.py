"""Run the integration against a real shield-manager web server with fake Shields."""

import threading
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.shield_manager.const import CONF_APP_SLUG, CONF_URL, DOMAIN
from shield_manager import appinfo
from shield_manager.registry import Device, Registry
from shield_manager.web.server import create_server
from tests.fakes import FakeConnection


@pytest.fixture
def app(tmp_path, socket_enabled):
    """The Shield Manager app's web server, with two fake Shields and one unreachable."""
    registry = Registry(tmp_path / "devices.json")
    registry.add(Device("den", "10.0.0.2"))
    registry.add(Device("living", "10.0.0.3"))
    registry.add(Device("bedroom", "10.0.0.4"))
    registry.set_reference("den")
    installed = {
        "den": {"org.xbmc.kodi": (20, "20.0"), "com.plexapp.android": (5, "5")},
        "living": {"org.xbmc.kodi": (19, "19.0"), "com.retroarch": (7, "1.7")},
    }

    def connect(device):
        if device.name not in installed:
            raise ConnectionRefusedError("unreachable")
        conn = FakeConnection(installed=installed[device.name])
        conn.installed = installed[device.name]
        return conn

    def fake_meta(conn, package, version_code, cache_dir):
        return appinfo.AppMeta(package, version_code, label=package.split(".")[1].title())

    with patch.object(appinfo, "fetch_meta", fake_meta):
        server = create_server(
            registry, port=0, connect=connect, cache_dir=tmp_path / "cache", downloads=None
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.url = f"http://127.0.0.1:{server.server_port}"
        server.registry, server.installed = registry, installed
        yield server
        server.shutdown()
        server.server_close()


async def _setup(hass: HomeAssistant, app) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={CONF_URL: app.url})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # App names are read in the background after the first look at the Shields.
    await hass.async_add_executor_job(_wait_for_names, app)
    await entry.runtime_data.async_refresh()
    return entry


async def test_entities_show_each_shield(hass: HomeAssistant, app) -> None:
    await _setup(hass, app)

    assert hass.states.get("sensor.living_apps_installed").state == "2"
    behind = hass.states.get("sensor.living_apps_behind_reference")
    assert behind.state == "2"  # Plex missing, Kodi outdated
    assert behind.attributes["missing"] == ["Plexapp"]
    assert behind.attributes["outdated"] == ["Xbmc"]
    assert behind.attributes["not_on_reference"] == ["Retroarch"]
    assert hass.states.get("sensor.den_apps_behind_reference").state == "0"

    assert hass.states.get("binary_sensor.living_reachable").state == "on"
    bedroom = hass.states.get("binary_sensor.bedroom_reachable")
    assert bedroom.state == "off"
    assert "unreachable" in bedroom.attributes["error"]
    assert hass.states.get("sensor.bedroom_apps_installed").state == "unknown"

    reference = hass.states.get("select.shield_manager_reference_shield")
    assert reference.state == "den"
    assert reference.attributes["options"] == ["bedroom", "den", "living"]
    assert hass.states.get("sensor.shield_manager_activity").state == "Idle"
    assert hass.states.get("sensor.shield_manager_app_updates_available").state == "0"


async def test_select_changes_reference(hass: HomeAssistant, app) -> None:
    await _setup(hass, app)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.shield_manager_reference_shield", "option": "living"},
        blocking=True,
    )

    assert app.registry.reference == "living"
    assert hass.states.get("select.shield_manager_reference_shield").state == "living"
    # Now den is the one behind: it lacks RetroArch.
    assert hass.states.get("sensor.den_apps_behind_reference").attributes["missing"] == [
        "Retroarch"
    ]


async def test_new_shields_get_entities(hass: HomeAssistant, app) -> None:
    entry = await _setup(hass, app)
    app.registry.add(Device("attic", "10.0.0.5"))
    app.installed["attic"] = {"com.plexapp.android": (5, "5")}

    entry.runtime_data.reread_shields()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.attic_apps_behind_reference").attributes["missing"] == ["Xbmc"]


async def test_sync_button_installs_from_reference(hass: HomeAssistant, app) -> None:
    await _setup(hass, app)

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.living_sync_from_reference"}, blocking=True
    )
    job = app.api.list_jobs()[0]
    assert job["title"] == "Sync from den"
    await hass.async_add_executor_job(_wait, app, job["id"])

    assert app.installed["living"]["org.xbmc.kodi"][0] == 20
    assert "com.plexapp.android" in app.installed["living"]
    assert "com.retroarch" in app.installed["living"]  # sync never removes


async def test_install_action_returns_the_job(hass: HomeAssistant, app) -> None:
    await _setup(hass, app)

    response = await hass.services.async_call(
        DOMAIN,
        "install",
        {"packages": "com.retroarch", "devices": ["den"]},
        blocking=True,
        return_response=True,
    )

    assert response["title"] == "Install Retroarch on den"
    await hass.async_add_executor_job(_wait, app, response["job_id"])
    assert "com.retroarch" in app.installed["den"]


async def test_errors_from_the_app_reach_the_user(hass: HomeAssistant, app) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await _setup(hass, app)

    with pytest.raises(HomeAssistantError, match="no device named 'attic'"):
        await hass.services.async_call(
            DOMAIN, "sync", {"devices": ["attic"]}, blocking=True, return_response=True
        )


async def test_user_flow(hass: HomeAssistant, app) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://127.0.0.1:1"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: app.url + "/"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: app.url}


async def test_found_from_the_app(hass: HomeAssistant, app) -> None:
    host, port = app.url.removeprefix("http://").split(":")
    info = HassioServiceInfo(
        config={"host": host, "port": int(port), "addon": "Shield Manager"},
        name="Shield Manager",
        slug="a1b2c3d4_shield_manager",
        uuid="1234",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=info
    )
    assert result["step_id"] == "hassio_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: app.url, CONF_APP_SLUG: "a1b2c3d4_shield_manager"}


def _wait(app, job_id: str) -> None:
    import time

    for _ in range(250):
        if app.api.job(job_id)["state"] != "running":
            return
        time.sleep(0.02)
    raise AssertionError("job didn't finish")


def _wait_for_names(app) -> None:
    import time

    for _ in range(250):
        if not app.api.meta_index()["pending"]:
            return
        time.sleep(0.02)
    raise AssertionError("app names weren't read")
