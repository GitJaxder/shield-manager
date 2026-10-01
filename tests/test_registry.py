import pytest

from shield_manager.registry import (
    Device,
    DeviceExistsError,
    DeviceNotFoundError,
    GroupNotFoundError,
    Registry,
)


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


def test_groups_persist_sorted_and_deduplicated(registry):
    registry.add(Device("den", "10.0.0.2", groups=("upstairs", "kids", "upstairs")))
    assert Registry(registry.path).get("den").groups == ("kids", "upstairs")


def test_set_groups(registry):
    registry.add(Device("den", "10.0.0.2", groups=("kids",)))
    registry.set_groups("den", ["living"])
    assert registry.get("den").groups == ("living",)


def test_resolve_by_name_group_and_all(registry):
    registry.add(Device("den", "10.0.0.2", groups=("upstairs",)))
    registry.add(Device("bedroom", "10.0.0.3", groups=("upstairs",)))
    registry.add(Device("garage", "10.0.0.4"))
    assert [d.name for d in registry.resolve(groups=["upstairs"])] == ["bedroom", "den"]
    assert [d.name for d in registry.resolve(["garage"], ["upstairs"])] == [
        "bedroom",
        "den",
        "garage",
    ]
    assert len(registry.resolve(all_=True)) == 3


def test_resolve_unknown_group_raises(registry):
    with pytest.raises(GroupNotFoundError):
        registry.resolve(groups=["nope"])
