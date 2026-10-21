"""Phase 1 of the installable distribution (#65): package version + console entrypoint."""

from __future__ import annotations

import pytest

import agent_sessions
from agent_sessions import cli, version

# version._git_sha resolves off __file__ — only a real install exercises that path.
pytestmark = pytest.mark.deploy_shape


def test_get_version_returns_a_nonempty_string():
    v = version.get_version()
    assert isinstance(v, str) and v
    # Dev/source checkout reports the placeholder, optionally + a git short SHA.
    assert v == "0.0.0" or v.startswith("0.0.0+") or v[0].isdigit()


def test_package_exposes_version():
    assert agent_sessions.__version__ == version.get_version()


def test_cli_version_subcommand(capsys):
    rc = cli.main(["version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == agent_sessions.__version__


def test_cli_no_subcommand_prints_help_and_returns_1(capsys):
    rc = cli.main([])
    assert rc == 1
    assert "usage:" in capsys.readouterr().out.lower()


def test_cli_serve_parser_defaults_to_localhost(monkeypatch):
    # `serve` binds localhost by default (the installer documents a reverse proxy/TLS).
    # The argparse default reads AGENT_SESSIONS_HOST from the env, so isolate it — a
    # runner with the env exported (self-hosted runners often have it for local serve)
    # otherwise contaminates the assertion. Same story for the port default.
    monkeypatch.delenv("AGENT_SESSIONS_HOST", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_PORT", raising=False)
    args = cli._build_parser().parse_args(["serve"])
    assert args.cmd == "serve"
    assert args.host == "127.0.0.1"
    assert isinstance(args.port, int)


def test_pyproject_uses_dynamic_scm_version():
    # Guard against regressing to a static version = "0.0.0" (which would freeze
    # /api/version and break the self-update foundation). Version must be SCM-derived.
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert "version" not in data["project"], "static [project].version would freeze the version"
    assert "version" in data["project"].get("dynamic", [])
    assert "setuptools_scm" in data.get("tool", {})
