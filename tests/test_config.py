from __future__ import annotations

import importlib
import os


def test_env_helpers(monkeypatch):
    # Import fresh so helpers are defined.
    import config

    # _env_str
    monkeypatch.delenv("X_STR", raising=False)
    assert config._env_str("X_STR", "d") == "d"
    monkeypatch.setenv("X_STR", "")
    assert config._env_str("X_STR", "d") == "d"
    monkeypatch.setenv("X_STR", "abc")
    assert config._env_str("X_STR", "d") == "abc"

    # _env_int
    monkeypatch.delenv("X_INT", raising=False)
    assert config._env_int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "  ")
    assert config._env_int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "12")
    assert config._env_int("X_INT", 7) == 12
    monkeypatch.setenv("X_INT", "0x10")
    assert config._env_int("X_INT", 7) == 16
    monkeypatch.setenv("X_INT", "nope")
    assert config._env_int("X_INT", 7) == 7

    # _env_float
    monkeypatch.delenv("X_FLOAT", raising=False)
    assert config._env_float("X_FLOAT", 1.25) == 1.25
    monkeypatch.setenv("X_FLOAT", "")
    assert config._env_float("X_FLOAT", 1.25) == 1.25
    monkeypatch.setenv("X_FLOAT", "2.5")
    assert config._env_float("X_FLOAT", 1.25) == 2.5
    monkeypatch.setenv("X_FLOAT", "bad")
    assert config._env_float("X_FLOAT", 1.25) == 1.25

    # _env_bool
    monkeypatch.delenv("X_BOOL", raising=False)
    assert config._env_bool("X_BOOL", True) is True
    monkeypatch.setenv("X_BOOL", "0")
    assert config._env_bool("X_BOOL", True) is False
    monkeypatch.setenv("X_BOOL", "1")
    assert config._env_bool("X_BOOL", False) is True
    monkeypatch.setenv("X_BOOL", "yes")
    assert config._env_bool("X_BOOL", False) is True
    monkeypatch.setenv("X_BOOL", "off")
    assert config._env_bool("X_BOOL", True) is False
    monkeypatch.setenv("X_BOOL", "maybe")
    assert config._env_bool("X_BOOL", True) is True


def test_config_import_is_stable(monkeypatch):
    # Ensure that importing config with different env values works and doesn't crash.
    monkeypatch.setenv("CAN_INTERFACE", "socketcan")
    import config
    importlib.reload(config)
    assert isinstance(config.CAN_INTERFACE, str)
