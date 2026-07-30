"""Tests for the Home Free agent entrypoint config assembly (no live relay)."""

from pathlib import Path

import pytest

from agent_sessions.homefree.__main__ import ConfigError, build_config_from_env


def test_build_config_from_inline_env():
    env = {
        "AGENT_SESSIONS_RELAY_URL": "wss://relay.example/relay/ws",
        "HOMEFREE_CONSOLE_NAME": "viper-8231",
        "HOMEFREE_ACCESS_KEY": "abcdef0123456789abcdef0123456789",
        "HOMEFREE_IDENTITY_PATH": "/tmp/homefree/identity",
    }
    cfg = build_config_from_env(env)
    assert cfg.relay_url == "wss://relay.example/relay/ws"
    assert cfg.console_name == "viper-8231"
    assert cfg.access_key == "abcdef0123456789abcdef0123456789"
    assert cfg.identity_path == Path("/tmp/homefree/identity")


def _base_env(**extra):
    return {
        "AGENT_SESSIONS_RELAY_URL": "wss://relay.example/relay/ws",
        "HOMEFREE_CONSOLE_NAME": "viper-8231",
        "HOMEFREE_ACCESS_KEY": "abcdef0123456789abcdef0123456789",
        "HOMEFREE_IDENTITY_PATH": "/tmp/homefree/identity",
        **extra,
    }


def test_app_mode_refuses_viewers_without_app_port():
    # No HOMEFREE_APP_PORT -> app_port None -> viewer sessions are refused.
    cfg = build_config_from_env(_base_env())
    assert cfg.app_port is None
    assert cfg.app_host == "127.0.0.1"


def test_app_port_enables_app_mode_with_defaults():
    cfg = build_config_from_env(_base_env(HOMEFREE_APP_PORT="8765"))
    assert cfg.app_port == 8765
    assert cfg.app_host == "127.0.0.1"  # loopback by default
    assert cfg.app_origin is None


def test_app_host_and_origin_parsed():
    cfg = build_config_from_env(
        _base_env(
            HOMEFREE_APP_PORT="3402",
            HOMEFREE_APP_HOST="127.0.0.1",
            HOMEFREE_APP_ORIGIN="http://127.0.0.1:3402",
        )
    )
    assert cfg.app_port == 3402
    assert cfg.app_origin == "http://127.0.0.1:3402"


@pytest.mark.parametrize("bad", ["notaport", "80x", ""])
def test_invalid_app_port_non_integer(bad):
    # "" → treated as unset (fail-closed); non-numeric → ConfigError.
    if bad == "":
        assert build_config_from_env(_base_env(HOMEFREE_APP_PORT=bad)).app_port is None
    else:
        with pytest.raises(ConfigError):
            build_config_from_env(_base_env(HOMEFREE_APP_PORT=bad))


@pytest.mark.parametrize("bad", ["0", "65536", "99999"])
def test_app_port_out_of_range(bad):
    with pytest.raises(ConfigError):
        build_config_from_env(_base_env(HOMEFREE_APP_PORT=bad))


def test_file_values_preferred_and_stripped(tmp_path):
    (tmp_path / "name").write_text("falcon-4412\n")
    (tmp_path / "key").write_text("  deadbeefdeadbeefdeadbeefdeadbeef  \n")
    env = {
        "AGENT_SESSIONS_RELAY_URL": "wss://r/relay/ws",
        "HOMEFREE_CONSOLE_NAME_FILE": str(tmp_path / "name"),
        "HOMEFREE_ACCESS_KEY_FILE": str(tmp_path / "key"),
        # inline values are ignored when *_FILE is present
        "HOMEFREE_CONSOLE_NAME": "ignored",
        "HOMEFREE_ACCESS_KEY": "ignored",
        "HOMEFREE_IDENTITY_PATH": str(tmp_path / "identity"),
    }
    cfg = build_config_from_env(env)
    assert cfg.console_name == "falcon-4412"
    assert cfg.access_key == "deadbeefdeadbeefdeadbeefdeadbeef"


@pytest.mark.parametrize(
    "missing",
    [
        "AGENT_SESSIONS_RELAY_URL",
        "HOMEFREE_CONSOLE_NAME",
        "HOMEFREE_ACCESS_KEY",
        "HOMEFREE_IDENTITY_PATH",
    ],
)
def test_missing_required_config_raises(missing):
    env = {
        "AGENT_SESSIONS_RELAY_URL": "wss://r/relay/ws",
        "HOMEFREE_CONSOLE_NAME": "viper-8231",
        "HOMEFREE_ACCESS_KEY": "abcdef0123456789abcdef0123456789",
        "HOMEFREE_IDENTITY_PATH": "/tmp/id",
    }
    del env[missing]
    with pytest.raises(ConfigError):
        build_config_from_env(env)
