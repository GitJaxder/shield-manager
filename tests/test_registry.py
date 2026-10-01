import pytest

from shield_manager.registry import Device, DeviceExistsError, DeviceNotFoundError, Registry


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "devices.json")


def test_empty_registry_lists_nothing(registry):
    assert registry.list() == []


def test_add_persists_across_instances(registry):
    registry.add(Device("living-room", "192.168.1.20"))
    reloaded = Registry(registry.path)
    assert reloaded.get("living-room") == Device("living-room", "192.168.1.20", 5555)


def test_list_is_sorted_by_name(registry):
    registry.add(Device("den", "10.0.0.2"))
    registry.add(Device("bedroom", "10.0.0.3"))
    assert [d.name for d in registry.list()] == ["bedroom", "den"]


def test_add_duplicate_raises(registry):
    registry.add(Device("den", "10.0.0.2"))
    with pytest.raises(DeviceExistsError):
        registry.add(Device("den", "10.0.0.9"))


def test_remove(registry):
    registry.add(Device("den", "10.0.0.2"))
    registry.remove("den")
    assert registry.list() == []


def test_remove_missing_raises(registry):
    with pytest.raises(DeviceNotFoundError):
        registry.remove("nope")


def test_address():
    assert Device("den", "10.0.0.2", 5556).address == "10.0.0.2:5556"
