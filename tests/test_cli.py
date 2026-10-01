import pytest

from shield_manager.cli import main
from shield_manager.registry import Registry


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "devices.json")


def test_device_add_and_list(registry, capsys):
    assert main(["device", "add", "den", "10.0.0.2"], registry=registry) == 0
    assert main(["device", "list"], registry=registry) == 0
    out = capsys.readouterr().out
    assert "den\t10.0.0.2:5555" in out


def test_list_empty_shows_hint(registry, capsys):
    assert main(["device", "list"], registry=registry) == 0
    assert "No devices registered" in capsys.readouterr().out


def test_add_duplicate_fails(registry, capsys):
    main(["device", "add", "den", "10.0.0.2"], registry=registry)
    assert main(["device", "add", "den", "10.0.0.3"], registry=registry) == 1
    assert "already registered" in capsys.readouterr().err


def test_remove_missing_fails(registry, capsys):
    assert main(["device", "remove", "nope"], registry=registry) == 1
    assert "no device named 'nope'" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "shield-manager" in capsys.readouterr().out
