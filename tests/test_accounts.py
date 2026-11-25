"""Admin credential management (#65 Phase 4a): env writer + reset-password."""

from __future__ import annotations

from agent_sessions import accounts, cli, envfile
from agent_sessions.auth import verify_password


def test_envfile_update_sets_drops_and_preserves(tmp_path):
    p = tmp_path / "env"
    p.write_text("KEEP=1\nDROP_ME=old\nCHANGE=old\n")
    envfile.update(p, {"CHANGE": "new", "DROP_ME": None, "ADD": "x"})
    text = p.read_text()
    assert "KEEP=1" in text  # untouched
    assert "CHANGE=new" in text  # replaced
    assert "ADD=x" in text  # added
    assert "DROP_ME" not in text  # dropped
    assert oct(p.stat().st_mode & 0o777) == "0o600"  # secure


def test_set_password_writes_hash_and_clears_force_flag(tmp_path):
    p = tmp_path / "env"
    p.write_text(
        "AGENT_SESSIONS_USERNAME=admin\n"
        "AGENT_SESSIONS_PASSWORD_HASH=pbkdf2_sha256$1$aa$bb\n"
        "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1\n"
    )
    accounts.set_password(p, "s3cret-new")
    text = p.read_text()
    assert "AGENT_SESSIONS_USERNAME=admin" in text  # preserved
    assert "AGENT_SESSIONS_FORCE_PASSWORD_CHANGE" not in text  # flag cleared
    hash_line = next(
        ln for ln in text.splitlines() if ln.startswith("AGENT_SESSIONS_PASSWORD_HASH=")
    )
    assert verify_password("s3cret-new", hash_line.split("=", 1)[1])  # new password verifies


def test_reset_password_cli_generates_and_prints_once(tmp_path, capsys):
    p = tmp_path / "env"
    p.write_text("AGENT_SESSIONS_PASSWORD_HASH=pbkdf2_sha256$1$aa$bb\n")
    rc = cli.main(["reset-password", "--env", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "new password:" in out
    pw = out.split("new password:", 1)[1].strip()
    hash_line = next(
        ln for ln in p.read_text().splitlines() if ln.startswith("AGENT_SESSIONS_PASSWORD_HASH=")
    )
    assert verify_password(pw, hash_line.split("=", 1)[1])


def test_reset_password_cli_stdin_not_echoed(tmp_path, capsys, monkeypatch):
    import io

    p = tmp_path / "env"
    p.write_text("AGENT_SESSIONS_PASSWORD_HASH=x\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("chosen-pw\n"))
    cli.main(["reset-password", "--env", str(p), "--stdin"])
    out = capsys.readouterr().out
    assert "chosen-pw" not in out  # a chosen password (via stdin) is never echoed
    hash_line = next(
        ln for ln in p.read_text().splitlines() if ln.startswith("AGENT_SESSIONS_PASSWORD_HASH=")
    )
    assert verify_password("chosen-pw", hash_line.split("=", 1)[1])


def test_reset_password_cli_missing_env_errors(tmp_path):
    assert cli.main(["reset-password", "--env", str(tmp_path / "nope")]) == 1
