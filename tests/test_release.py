"""Every place that carries the version must agree, or a release ships mismatched parts:
HACS reads the integration's manifest, Home Assistant's app store reads the app's config
(and builds the matching release tag), and pip reads pyproject.toml."""

import json
import re
from pathlib import Path

import shield_manager

ROOT = Path(__file__).resolve().parent.parent


def _match(pattern: str, path: str) -> str:
    found = re.search(pattern, (ROOT / path).read_text(), re.MULTILINE)
    assert found, f"no version in {path}"
    return found.group(1)


def test_versions_match():
    version = _match(r'^version = "([^"]+)"', "pyproject.toml")
    manifest = json.loads((ROOT / "custom_components/shield_manager/manifest.json").read_text())
    assert shield_manager.__version__ == version
    assert manifest["version"] == version
    assert _match(r'^version: "([^"]+)"', "ha-app/shield_manager/config.yaml") == version


def test_changelog_has_the_version():
    assert _match(r"^## (\S+)", "CHANGELOG.md") == _match(r'^version = "([^"]+)"', "pyproject.toml")
