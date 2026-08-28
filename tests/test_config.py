"""Tests for configuration-schema validation."""
# ruff: noqa: DOC201  (return sections are noise in small test helpers)

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mergedeep import merge
from sc_foundation import SCConfigManager
from sc_smart_device import smart_devices_validator

from config_schemas import ConfigSchema

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

_VALID_CONFIG = """
General:
  AppName: eufy-siren
  PollingInterval: 10
Files:
  LogfileName: logs/logfile.log
  LogfileVerbosity: debug
  ConsoleVerbosity: debug
SCSmartDevices:
  Devices:
    - Name: Test Siren
      Model: Shelly2PMG3
      Simulate: True
      Outputs:
        - Name: "Siren O1"
ServiceAPI:
  Enable: True
  HostingIP: 0.0.0.0
  Port: 8085
  Endpoints:
    - Name: "Camera 1"
      Path: "/motion/camera1"
      Action: "Motion"
Siren:
  Enable: True
  Switch: "Siren O1"
  SirenDuration: 30
  MinMotionEvents: 2
  MinMotionSources: 2
  MinMotionInterval: 10
  MaxMotionInterval: 60
  PostTriggerSleepTimer: 60
"""


def _merged_schema() -> dict:
    """Build the same merged validation schema main.py uses.

    Note: the Files/Email/HeartbeatMonitor schema is merged in automatically by
    SCConfigManager, so it is intentionally not included here.
    """
    schema = merge({}, ConfigSchema().validation, smart_devices_validator)
    assert isinstance(schema, dict)
    return schema


def _write_config(make: Callable[[str], Path], text: str) -> Path:
    """Write config text to a temp file and return its path."""
    path = make("config.yaml")
    path.write_text(text)
    return path


@pytest.fixture
def tmp_config(tmp_path: Path) -> Callable[[str], Path]:
    """Return a factory that creates named files under a temp directory."""
    return lambda name: tmp_path / name


def test_valid_config_passes(tmp_config: Callable[[str], Path]) -> None:
    """A well-formed config validates without error."""
    path = _write_config(tmp_config, _VALID_CONFIG)
    config = SCConfigManager(config_file=str(path), validation_schema=_merged_schema())
    assert config.get("Siren", "Switch") == "Siren O1"


def test_invalid_endpoint_action_fails(tmp_config: Callable[[str], Path]) -> None:
    """An unknown endpoint action is rejected by the schema."""
    bad = _VALID_CONFIG.replace('Action: "Motion"', 'Action: "Explode"')
    path = _write_config(tmp_config, bad)
    with pytest.raises(RuntimeError):
        SCConfigManager(config_file=str(path), validation_schema=_merged_schema())


def test_out_of_range_port_fails(tmp_config: Callable[[str], Path]) -> None:
    """A port outside the allowed range is rejected."""
    bad = _VALID_CONFIG.replace("Port: 8085", "Port: 70000")
    path = _write_config(tmp_config, bad)
    with pytest.raises(RuntimeError):
        SCConfigManager(config_file=str(path), validation_schema=_merged_schema())


def test_shipped_development_config_is_valid() -> None:
    """The committed development config validates against the merged schema."""
    dev_config = REPO_ROOT / "configs" / "development.yaml"
    config = SCConfigManager(config_file=str(dev_config), validation_schema=_merged_schema())
    assert config.get("ServiceAPI", "Port") == 8085
