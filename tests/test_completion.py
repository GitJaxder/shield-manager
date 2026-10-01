import os
import subprocess
import sys

import pytest

from shield_manager import completion
from shield_manager.cli import main
from shield_manager.registry import Device, Registry


@pytest.fixture
def registry():
    reg = Registry()
    reg.add(Device("living-room", "10.0.0.1", groups=("downstairs",)))
    reg.add(Device("den", "10.0.0.2", groups=("upstairs",)))
    return reg


def test_complete_devices_and_groups(registry):
    assert completion.complete_devices("l") == ["living-room"]
    assert completion.complete_groups("") == ["downstairs", "upstairs"]


def test_package_cache_merges_and_filters():
    completion.remember_packages(["org.xbmc.kodi", "com.plexapp.android"])
    completion.remember_packages(["org.xbmc.kodi", "org.videolan.vlc"])
    assert completion.known_packages() == [
        "com.plexapp.android",
        "org.videolan.vlc",
        "org.xbmc.kodi",
    ]
    assert completion.complete_packages("org.v") == ["org.videolan.vlc"]


def test_missing_or_corrupt_cache_is_empty(isolated_config):
    assert completion.known_packages() == []
    isolated_config.mkdir(parents=True)
    completion.package_cache_path().write_text("not json")
    assert completion.known_packages() == []


def test_listing_apps_fills_the_cache(registry, monkeypatch):
    from shield_manager import adb
    from tests.fakes import FakeConnection

    conn = FakeConnection(responses={"pm list packages": "package:org.xbmc.kodi\n"})
    monkeypatch.setattr(adb, "connect", lambda device: conn)
    assert main(["app", "list", "-d", "den"]) == 0
    assert completion.known_packages() == ["org.xbmc.kodi"]
    assert conn.closed


@pytest.mark.parametrize("shell", completion.SHELLS)
def test_completion_command_prints_script(shell, capsys):
    assert main(["completion", shell]) == 0
    assert "shield-manager" in capsys.readouterr().out


def _complete(line, home, tmp_path):
    """Ask the real CLI for completions the way a shell hook does."""
    out = tmp_path / "completions.txt"
    env = {
        **os.environ,
        "SHIELD_MANAGER_HOME": str(home),
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\n",
        "ARGCOMPLETE_USE_TEMPFILES": "1",
        "_ARGCOMPLETE_STDOUT_FILENAME": str(out),
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
    }
    subprocess.run([sys.executable, "-m", "shield_manager"], env=env, check=False)
    return sorted(filter(None, out.read_text().split("\n")))


def test_end_to_end_completion(registry, isolated_config, tmp_path):
    completion.remember_packages(["org.xbmc.kodi", "org.videolan.vlc", "com.plexapp.android"])
    assert _complete("shield-manager app uninstall org.", isolated_config, tmp_path) == [
        "org.videolan.vlc",
        "org.xbmc.kodi",
    ]
    assert _complete("shield-manager fleet status --from l", isolated_config, tmp_path) == [
        "living-room "
    ]
