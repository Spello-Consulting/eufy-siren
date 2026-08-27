"""Tests for src/main.py."""

import sys

import pytest

from main import parse_command_line_args


def test_parse_command_line_args_defaults(monkeypatch, tmp_path):
    """With no arguments, the default config file and no homedir are returned."""
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr("main.SCCommon.get_project_root", lambda: tmp_path)

    result = parse_command_line_args()

    assert result == {"config_file": "config.yaml", "homedir": None}


def test_parse_command_line_args_with_valid_config(monkeypatch, tmp_path):
    """A valid --config path is resolved to an absolute path."""
    config_file = tmp_path / "myconfig.yaml"
    config_file.write_text("General:\n")
    monkeypatch.setattr(sys, "argv", ["main.py", "--homedir", str(tmp_path), "--config", "myconfig.yaml"])

    result = parse_command_line_args()

    assert result["config_file"] == str(config_file.resolve())
    assert result["homedir"] == str(tmp_path.resolve())


def test_parse_command_line_args_missing_config_exits(monkeypatch, tmp_path):
    """A --config path that doesn't exist causes the program to exit."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--homedir", str(tmp_path), "--config", "missing.yaml"])

    with pytest.raises(SystemExit) as exc_info:
        parse_command_line_args()

    assert exc_info.value.code == 1


def test_parse_command_line_args_missing_homedir_exits(monkeypatch, tmp_path):
    """A --homedir path that doesn't exist causes the program to exit."""
    missing_dir = tmp_path / "does-not-exist"
    monkeypatch.setattr(sys, "argv", ["main.py", "--homedir", str(missing_dir)])

    with pytest.raises(SystemExit) as exc_info:
        parse_command_line_args()

    assert exc_info.value.code == 1
