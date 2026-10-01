import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep every test's registry, ADB key and app-name cache out of the real home dir."""
    home = tmp_path / "shield-manager-home"
    monkeypatch.setenv("SHIELD_MANAGER_HOME", str(home))
    return home
