"""#1397 P4 — calling HQ on this machine: `agent-sessions voice setup` and the local speech server.

Pinned here:

* every model is pinned by size + sha256, every locked package by hash; a mismatch aborts and
  leaves no file;
* a failed setup changes nothing active (`current`, stamp's `active`, the token); a good one swaps
  `current` atomically and keeps the previous generation for `--rollback`;
* the token lives in a 0600 env file, never in the unit, argv or a response; nothing is written
  outside `AGENT_SESSIONS_HOME` under `AGENT_SESSIONS_NO_SERVICE=1`;
* `remove` refuses while a call holds the call lock, and keeps the models unless asked;
* the speech server refuses a missing/wrong token, bounds its input, and passes the P0
  compatibility check in-process (fake models);
* "Use this machine" saves both endpoints server-side and never returns the token;
* the worker runs THIS release's package through its shim; an explicit interpreter override wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import re
import stat
import struct
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    hq_calls_settings,
    speech_conformance,
    speech_endpoints,
    speech_server,
    voice_calls,
)
from agent_sessions import voice_setup as vs

TOKEN = "t" * 64
STUCK: list[bool] = []


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_NO_SERVICE", "1")
    monkeypatch.delenv("AGENT_SESSIONS_VOICE_PYTHON", raising=False)
    # No unit test talks to a real local server (one may be listening on 127.0.0.1:8766).
    monkeypatch.setattr(vs, "probe", lambda token, wait_s=60.0: False)
    # A stuck synthesis exits the server for a restart; never the test process.
    STUCK.clear()
    monkeypatch.setattr(speech_server, "ON_STUCK", lambda: STUCK.append(True), raising=False)


def _art(data: bytes, name="m.bin") -> vs.Artifact:
    return vs.Artifact(name, f"https://example.invalid/{name}", len(data),
                       hashlib.sha256(data).hexdigest())  # fmt: skip


def _fetch_of(data: bytes):
    @contextlib.contextmanager
    def fetch(url):
        yield iter([data[i : i + 7] for i in range(0, len(data), 7)])

    return fetch


# --- pins ------------------------------------------------------------------------------------


@pytest.mark.parametrize("stt", vs.STT_MODELS)
def test_every_model_is_pinned(stt):
    for a in vs.models_for(stt):
        assert a.url.startswith("https://") and a.size > 0
        assert re.fullmatch(r"[0-9a-f]{64}", a.sha256)
    # Hugging Face files are fetched at a fixed revision, never `main`.
    assert all("/resolve/main/" not in a.url for a in vs.models_for(stt))


@pytest.mark.parametrize("kind", vs.KINDS)
def test_every_locked_package_carries_hashes(kind):
    text = (vs.LOCKS / f"{kind}.txt").read_text()
    reqs = re.split(r"\n(?=[A-Za-z0-9])", text.strip())
    assert len(reqs) > 10
    for r in reqs:
        assert "==" in r.splitlines()[0] and "--hash=sha256:" in r, r.splitlines()[0]


def test_the_server_lock_pins_one_pyav_that_faster_whisper_can_call():
    """faster-whisper 1.2.1 decodes with `av.open(..., metadata_errors=...)`, which PyAV 19
    removed: a PyAV 19 for Python >= 3.12 made every transcription 400. ONE PyAV, below 19, for
    every Python."""
    text = (vs.LOCKS / "server.txt").read_text()
    pins = re.findall(r"^av==(\S+)(.*)$", text, re.M)
    assert len(pins) == 1, pins
    version, marker = pins[0]
    assert int(version.split(".")[0]) < 19 and "python_full_version" not in marker


def test_the_server_check_decodes_audio_not_only_imports():
    """The setup's check runs the decode a transcription runs, so an API break fails the setup
    (which keeps the previous set) instead of every call."""
    check = vs._VERIFY["server"]
    assert "decode_audio" in check and "wave" in check


# --- downloads -------------------------------------------------------------------------------


def test_a_verified_download_lands(tmp_path):
    data = b"model bytes" * 100
    out = vs.download(_art(data), tmp_path, fetch=_fetch_of(data), say=lambda m: None)
    assert out.read_bytes() == data
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("bad", [b"model bytes" * 99 + b"X" * 11, b"short"])
def test_a_mismatch_aborts_and_leaves_no_file(tmp_path, bad):
    good = b"model bytes" * 100
    with pytest.raises(vs.SetupError):
        vs.download(_art(good), tmp_path, fetch=_fetch_of(bad), say=lambda m: None)
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_download_is_cut_off(tmp_path):
    good = b"x" * 50
    with pytest.raises(vs.SetupError, match="larger"):
        vs.download(_art(good), tmp_path, fetch=_fetch_of(good * 3), say=lambda m: None)
    assert list(tmp_path.iterdir()) == []


# --- setup as a transaction ------------------------------------------------------------------


def _fake_build(monkeypatch, fail_kind: str | None = None):
    def build(kind, log, python=None):
        if kind == fail_kind:
            raise vs.SetupError(f"{kind}: pip failed")
        gen = vs.root() / kind / vs.lock_sha(kind)
        (gen / "bin").mkdir(parents=True, exist_ok=True)
        (gen / "bin" / "python").write_text("")
        (gen / ".verified").write_text("ok")
        return gen

    monkeypatch.setattr(vs, "build", build)
    data = b"weights" * 10
    monkeypatch.setattr(vs, "models_for", lambda stt: [_art(data)])
    return _fetch_of(data)


def _prior_generation() -> str:
    """A working setup from an earlier release."""
    gens = {}
    for kind in vs.KINDS:
        old = vs.root() / kind / ("0" * 16)
        (old / "bin").mkdir(parents=True)
        (old / ".verified").write_text("ok")
        if kind == "server":
            (old / "speech_server.py").write_text("")
        gens[kind] = old.name
    name = vs.make_set(gens)
    vs.activate_set(name)
    vs._write_stamp({"active": gens, "set": name})
    vs._write_token("a" * 64)
    return name


def test_a_failed_setup_changes_nothing_active(monkeypatch):
    _prior_generation()
    fetch = _fake_build(monkeypatch, fail_kind="server")
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 2
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}
    st = vs.read_stamp()
    assert st["active"] == {k: "0" * 16 for k in vs.KINDS} and "pip failed" in st["last_error"]
    assert vs.read_token() == "a" * 64


def test_a_good_setup_swaps_and_keeps_the_previous(monkeypatch, tmp_path):
    _prior_generation()
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    st = vs.read_stamp()
    assert vs.active() == {k: vs.lock_sha(k) for k in vs.KINDS}
    assert st["previous_set"] == "0" * 16 + "-" + "0" * 16
    assert st["last_error"] is None and vs.stale() is None
    assert vs.rollback(say=lambda m: None) == 0
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}


def test_both_kinds_switch_in_one_rename(monkeypatch):
    """One pointer, so no crash point leaves a mixed runtime/server pair: every rename during
    setup either leaves the old set fully active or the new one."""
    old = _prior_generation()
    fetch = _fake_build(monkeypatch)
    seen = []
    real = os.replace

    def spy(a, b):
        real(a, b)
        if Path(b) == vs.root() / "current":
            seen.append(vs.active())

    monkeypatch.setattr(vs.os, "replace", spy)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    assert seen == [{k: vs.lock_sha(k) for k in vs.KINDS}]
    assert old != vs.active_set()


def test_a_server_that_fails_to_start_puts_the_previous_set_back(monkeypatch):
    _prior_generation()
    fetch = _fake_build(monkeypatch)
    restarts = []
    monkeypatch.setattr(vs, "install_unit", lambda stt: True)

    def restart(token):
        restarts.append(vs.active_set())
        return len(restarts) > 1  # the new set fails; the previous one comes back up

    monkeypatch.setattr(vs, "restart_verified", restart)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 2
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}
    assert restarts[1] == "0" * 16 + "-" + "0" * 16
    assert "did not start" in vs.read_stamp()["last_error"]


def test_setup_asks_first_and_does_nothing_on_no(monkeypatch):
    fetch = _fake_build(monkeypatch)
    assert vs.setup(fetch=fetch, say=lambda m: None, ask=lambda q: "n") == 1
    assert not vs.root().exists()


def test_the_token_file_and_unit(monkeypatch, tmp_path):
    fetch = _fake_build(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    env = vs.env_file()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    token = vs.read_token()
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert env.read_text() == f"SPEECH_SERVER_TOKEN={token}\n"
    assert stat.S_IMODE(vs.root().stat().st_mode) == 0o700
    # No user service under NO_SERVICE: nothing outside AGENT_SESSIONS_HOME was written.
    assert not (tmp_path / "xdg").exists()
    unit = vs.unit_text("small")
    assert f"EnvironmentFile={env}" in unit and token not in unit
    assert f"--uds {vs.socket_path()}" in unit and "--host" not in unit and "--port" not in unit
    assert "MemoryMax=3G" in unit and "CPUQuota=200%" in unit
    assert "NoNewPrivileges=yes" in unit


def test_a_lock_change_reads_as_stale(monkeypatch):
    _prior_generation()
    assert "another release" in vs.stale()


# --- remove ----------------------------------------------------------------------------------


def test_remove_refuses_during_a_call_and_keeps_models(monkeypatch):
    fetch = _fake_build(monkeypatch)
    vs.setup(yes=True, fetch=fetch, say=lambda m: None)
    with vs.call_lock():
        assert vs.remove(yes=True, say=lambda m: None) == 1
        assert vs.current("runtime").exists()
    assert vs.remove(yes=True, say=lambda m: None) == 0
    assert not (vs.root() / "runtime").exists() and vs.read_token() is None
    assert any((vs.root() / "models").iterdir())  # kept unless --models
    assert vs.remove(yes=True, models=True, say=lambda m: None) == 0
    assert not (vs.root() / "models").exists()


def test_a_call_is_refused_while_remove_holds_the_lock():
    vs.root().mkdir(parents=True)
    reg = voice_calls.Registry()

    async def go():
        with vs.no_call() as free:
            assert free
            with pytest.raises(voice_calls.CallError) as e:
                await reg.admit(cid="x", contact="europe-man", voice="v", operator=None,
                                app=None, registry=reg)  # fmt: skip
            assert e.value.status == 503
        call = await reg.admit(cid="x", contact="europe-man", voice="v", operator=None,
                               app=None, registry=reg)  # fmt: skip
        with vs.no_call() as free:
            assert not free  # the running call holds it
        call._held.close()  # what `Call.end` does
        with vs.no_call() as free:
            assert free

    asyncio.run(go())


# --- the speech server -----------------------------------------------------------------------


def _tone(seconds: float, rate: int) -> bytes:
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)
    )


class FakeSTT:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language):
        self.calls.append(language)
        return speech_conformance.PROBE_PHRASE, "en", 2.0


class FakeTTS:
    voices = ["af_heart", "am_michael"]
    languages = ["en", "es"]

    async def stream(self, text, voice, speed, lang):
        pcm = _tone(1.2, speech_server.TTS_RATE)
        for i in range(0, len(pcm), 9600):
            await asyncio.sleep(0)
            yield pcm[i : i + 9600]


def _server():
    return speech_server.create_app(TOKEN, FakeSTT(), FakeTTS())


def test_the_server_refuses_a_missing_or_wrong_token():
    c = TestClient(_server())
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/health", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = c.get("/v1/models", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and TOKEN not in ok.text


def test_the_server_bounds_its_input():
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    r = c.post("/v1/audio/speech", json={"input": "x" * 4001, "voice": "af_heart"})
    assert r.status_code == 413
    assert c.post("/v1/audio/speech", json={"input": "hi", "voice": "nobody"}).status_code == 400
    assert c.post("/v1/audio/speech", json={"input": "hi", "voice": "af_heart",
                                            "speed": 9}).status_code == 400  # fmt: skip
    big = b"\0" * (speech_server.UPLOAD_MAX + 70 * 1024)
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", big, "audio/wav")})
    assert r.status_code == 413


def test_the_server_has_no_tcp_listener_and_wants_an_owner_only_socket_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEECH_SERVER_TOKEN", TOKEN)
    assert speech_server.main(["--host", "0.0.0.0", "--models", "/nonexistent"]) == 2
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    assert speech_server.main(["--uds", str(open_dir / "s.sock"), "--models", "/x"]) == 2


def test_kokoro_language_mapping():
    assert speech_server.kokoro_lang("bf_emma", "en") == "en-gb"
    assert speech_server.kokoro_lang("af_heart", "es") == "es"
    assert speech_server.kokoro_lang("af_heart", None) == "en-us"  # none asked: the voice's own
    assert speech_server.kokoro_lang("bf_emma", None) == "en-gb"
    # #1716: a language Kokoro can't speak is refused, never read with English sounds
    assert speech_server.kokoro_lang("af_heart", "de") is None
    assert speech_server.kokoro_lang("af_heart", "ro") is None


@pytest.mark.parametrize("language", ["de", "ro", "xx"])
def test_the_server_refuses_a_language_it_cannot_speak(language):
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    r = c.post("/v1/audio/speech",
               json={"input": "Guten Tag.", "voice": "af_heart", "language": language})  # fmt: skip
    assert r.status_code == 400 and r.json() == {"error": {"code": "unsupported_language"}}


@pytest.mark.parametrize("language", [123, False, ["de"], {"de": 1}])
def test_a_malformed_language_is_refused_not_taken_as_none(language):
    """Hermes 7200: `{"language": 123}` must not speak as if no language had been asked for."""
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    r = c.post("/v1/audio/speech",
               json={"input": "Guten Tag.", "voice": "af_heart", "language": language})  # fmt: skip
    assert r.status_code == 400 and r.json() == {"error": {"code": "bad_language"}}


def test_json_null_is_no_language_like_an_absent_field():
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    r = c.post("/v1/audio/speech", json={"input": "Hello.", "voice": "af_heart", "language": None})
    assert r.status_code == 200 and len(r.content) > 0


@pytest.mark.parametrize("language", [None, "en", "es"])
def test_the_server_still_speaks_its_own_languages(language):
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    body = {"input": "Hello.", "voice": "af_heart", **({"language": language} if language else {})}
    r = c.post("/v1/audio/speech", json=body)
    assert r.status_code == 200 and len(r.content) > 0


@pytest.fixture
def _local_server(monkeypatch):
    app = _server()

    async def fake_resolve(host, port):
        return ["127.0.0.1"]

    monkeypatch.setattr(speech_endpoints, "_resolve", fake_resolve)
    monkeypatch.setattr(speech_endpoints, "_TRANSPORT", httpx.ASGITransport(app=app))
    return app


def test_the_server_passes_the_compatibility_check(_local_server):
    ep = lambda m: speech_endpoints.Endpoint(vs.LOCAL_URL, m, TOKEN)  # noqa: E731
    rep = asyncio.run(speech_conformance.check(ep("faster-whisper"), ep("kokoro-v1.0"), "af_heart"))
    ok = {c.id: c.ok for c in rep.checks}
    assert rep.usable, rep.as_dict()
    # (Streaming isn't observable through httpx's ASGI transport, which buffers; measured live.)
    assert ok["tts.voices"] and ok["stt.language"] and ok["tts.language"]


def test_use_this_machine_saves_both_sides_and_never_returns_the_token(
    auth_cfg, _local_server, monkeypatch
):
    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post("/login", data={"username": "marcus", "password": "hunter2"}, follow_redirects=False,
           headers={"Origin": auth_cfg.origin})  # fmt: skip
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    r = c.post("/api/voice/local/use", headers=hdr)
    assert r.status_code == 409 and r.json()["code"] == "not_set_up"
    vs._write_token(TOKEN)
    st = c.get("/api/voice/local").json()
    assert st["running"] is True and st["in_use"] is False and TOKEN not in str(st)
    r = c.post("/api/voice/local/use", headers=hdr)
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    block = hq_calls_settings.get()
    for side in ("stt", "tts"):
        assert block[side]["base_url"] == vs.LOCAL_URL and block[side]["api_key"] == TOKEN
    assert block["voices"] == vs.VOICES
    assert block["stt_detects_language"] and block["tts_accepts_language"]
    pub = c.get("/api/voice/settings").json()
    assert TOKEN not in str(pub) and pub["stt"]["api_key_set"] is True
    assert c.get("/api/voice/local").json()["in_use"] is True


# --- the worker ------------------------------------------------------------------------------


def test_the_worker_imports_this_release_through_its_shim(tmp_path):
    shim = voice_calls._shim(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c", "import agent_sessions.voice_text as m; print(m.__file__)"],
        env=voice_calls._worker_env({}, shim=shim), cwd=str(tmp_path), capture_output=True,
        text=True, check=True,
    )  # fmt: skip
    assert out.stdout.strip().startswith(str(tmp_path / "agent_sessions"))
    assert (tmp_path / "agent_sessions").resolve() == Path(voice_calls.__file__).resolve().parent


def test_the_worker_interpreter_preference(monkeypatch, tmp_path):
    assert voice_calls.worker_python() == sys.executable  # nothing set up
    py = tmp_path / "py"
    monkeypatch.setattr(vs, "runtime_python", lambda: py)
    assert voice_calls.worker_python() == str(py)
    monkeypatch.setenv("AGENT_SESSIONS_VOICE_PYTHON", "/opt/other/python")
    assert voice_calls.worker_python() == "/opt/other/python"


# --- token rotation --------------------------------------------------------------------------


def _local_settings():
    hq_calls_settings.save({
        "stt": {"base_url": vs.LOCAL_URL, "model": "faster-whisper", "api_key": "a" * 64},
        "tts": {"base_url": vs.LOCAL_URL, "model": "kokoro-v1.0", "api_key": "a" * 64},
    })  # fmt: skip


def test_rotation_moves_the_service_and_the_settings_together(monkeypatch):
    _prior_generation()
    _local_settings()
    seen = []
    monkeypatch.setattr(vs, "restart_verified", lambda t: seen.append(t) or True)
    assert vs.rotate_token(say=lambda m: None) == 0
    new = vs.read_token()
    assert new != "a" * 64 and seen == [new]
    block = hq_calls_settings.get()
    assert block["stt"]["api_key"] == new and block["tts"]["api_key"] == new


@pytest.mark.parametrize("fail", ["restart", "save"])
def test_a_failed_rotation_puts_the_old_token_back(monkeypatch, fail):
    _prior_generation()
    _local_settings()
    seen = []

    def restart(t):
        seen.append(t)
        return not (fail == "restart" and t != "a" * 64)

    def save(patch):
        raise hq_calls_settings.SettingsError("disk full")

    monkeypatch.setattr(vs, "restart_verified", restart)
    kw = {"save": save} if fail == "save" else {}
    assert vs.rotate_token(say=lambda m: None, **kw) == 2
    assert vs.read_token() == "a" * 64 and seen[-1] == "a" * 64  # file and service: the old one
    assert hq_calls_settings.get()["stt"]["api_key"] == "a" * 64


def test_rotation_is_refused_during_a_call(monkeypatch):
    _prior_generation()
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    with vs.call_lock():
        assert vs.rotate_token(say=lambda m: None) == 1
    assert vs.read_token() == "a" * 64


def test_the_worker_runs_the_set_up_runtime_after_each_step(monkeypatch):
    """Review 103978: the interpreter actually chosen, through the ONE pointer — after a first
    setup, a runtime-only and a server-only setup, and a rollback."""
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    py = Path(voice_calls.worker_python())
    assert py == vs.root() / "current" / "runtime" / "bin" / "python"
    assert py.resolve().parent.parent.name == vs.lock_sha("runtime")
    first = vs.active()

    # A runtime-only setup keeps the active server generation (and vice versa).
    monkeypatch.setattr(vs, "lock_sha", lambda k: ("1" if k == "runtime" else "0") * 16)
    assert vs.setup(yes=True, runtime=True, server=False, fetch=fetch, say=lambda m: None) == 0
    assert vs.active() == {"runtime": "1" * 16, "server": first["server"]}
    assert Path(voice_calls.worker_python()).resolve().parent.parent.name == "1" * 16
    monkeypatch.setattr(vs, "lock_sha", lambda k: ("1" if k == "runtime" else "2") * 16)
    assert vs.setup(yes=True, runtime=False, server=True, fetch=fetch, say=lambda m: None) == 0
    assert vs.active() == {"runtime": "1" * 16, "server": "2" * 16}

    # Rollback: the previous set, both kinds together.
    assert vs.rollback(say=lambda m: None) == 0
    assert vs.active() == {"runtime": "1" * 16, "server": first["server"]}
    assert Path(voice_calls.worker_python()).resolve().parent.parent.name == "1" * 16


def test_setup_never_switches_under_a_live_call(monkeypatch):
    """Review 104036: a call keeps its server and generations. Setup still downloads and verifies,
    but refuses to swap, restart or prune while a call holds the lock; the next run activates the
    verified build without re-downloading anything."""
    old = _prior_generation()
    fetch = _fake_build(monkeypatch)
    restarts = []
    monkeypatch.setattr(vs, "install_unit", lambda stt: True)
    monkeypatch.setattr(vs, "restart_verified", lambda t: restarts.append(t) or True)
    with vs.call_lock():
        assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 2
        assert vs.active_set() == old and restarts == []
        assert all((vs.root() / k / ("0" * 16) / ".verified").exists() for k in vs.KINDS)
        assert "call is running" in vs.read_stamp()["last_error"]
        assert vs.rollback(say=lambda m: None) == 1
    built = {k: (vs.root() / k / vs.lock_sha(k) / ".verified").exists() for k in vs.KINDS}
    assert built == {k: True for k in vs.KINDS}  # kept for the next run

    calls = []
    real_build = vs.build
    monkeypatch.setattr(
        vs, "build", lambda k, log, python=None: calls.append(k) or real_build(k, log)
    )
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    assert vs.active() == {k: vs.lock_sha(k) for k in vs.KINDS} and len(restarts) == 1


def test_two_upgrades_keep_the_active_and_the_rollback_generation(monkeypatch):
    _prior_generation()
    fetch = _fake_build(monkeypatch)
    for n in ("1", "2"):
        monkeypatch.setattr(vs, "lock_sha", lambda k, n=n: n * 16)
        assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    kept = {k: sorted(d.name for d in (vs.root() / k).iterdir() if d.is_dir()) for k in vs.KINDS}
    assert kept == {k: ["1" * 16, "2" * 16] for k in vs.KINDS}  # active + rollback; "0…" pruned


def test_without_the_service_the_limits_read_as_soft():
    """`voice serve` has no memory or CPU cap; status says so instead of implying the unit's."""
    assert vs.status()["limits"] == "soft"  # AGENT_SESSIONS_NO_SERVICE=1: no unit


def test_a_file_error_is_a_named_failure_and_changes_nothing(monkeypatch):
    """The 2026-10-23 drill: a source file vanished mid-build and setup died with a traceback."""
    old = _prior_generation()
    _fake_build(monkeypatch)

    def build(kind, log, python=None):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(vs, "build", build)
    data = b"weights" * 10
    assert vs.setup(yes=True, fetch=_fetch_of(data), say=lambda m: None) == 2
    assert vs.active_set() == old
    assert "FileNotFoundError" in vs.read_stamp()["last_error"]


def test_rotation_refuses_while_voice_serve_runs(monkeypatch):
    """The drill: without the user service, a running `voice serve` kept the old token while the
    settings got the new one. Rotation refuses while one holds its lifetime lock (review 6896:
    a lock, not a point-in-time probe — a serve still loading its models holds it too)."""
    _prior_generation()
    with vs.no_serve() as held:  # a `voice serve` (even one not yet listening)
        assert held
        assert vs.rotate_token(say=lambda m: None) == 1
    assert vs.read_token() == "a" * 64
    monkeypatch.setattr(vs, "restart_verified", lambda t: None)
    assert vs.rotate_token(say=lambda m: None) == 0 and vs.read_token() != "a" * 64


def test_remove_refuses_while_voice_serve_runs(monkeypatch):
    fetch = _fake_build(monkeypatch)
    vs.setup(yes=True, fetch=fetch, say=lambda m: None)
    with vs.no_serve():
        assert vs.remove(yes=True, say=lambda m: None) == 1
    assert vs.current("runtime").exists()


def test_rotation_keeps_the_local_servers_tested_claims(monkeypatch):
    """#1635 review: claims belong to a credential, and a token change resets them — but `voice
    rotate` changes ONLY the token of our own server, which it verified answers with the new one:
    its tested claims carry over (else detection would reset to unknown and spoken confirmation
    fail closed until someone re-tests)."""
    _prior_generation()
    for s, m in (("stt", "faster-whisper"), ("tts", "kokoro-v1.0")):
        hq_calls_settings.observe(s, vs.LOCAL_URL, m, "a" * 64, stt_detects_language=True,
                                  tts_accepts_language=True, tts_voices=["af_heart"])  # fmt: skip
    hq_calls_settings.save({
        "stt": {"base_url": vs.LOCAL_URL, "model": "faster-whisper", "api_key": "a" * 64},
        "tts": {"base_url": vs.LOCAL_URL, "model": "kokoro-v1.0", "api_key": "a" * 64},
        "stt_detects_language": True, "tts_accepts_language": True, "tts_voices": ["af_heart"],
    })  # fmt: skip
    hq_calls_settings.forget_observations()  # rotation runs in its own (CLI) process
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.rotate_token(say=lambda m: None) == 0
    block = hq_calls_settings.get()
    assert block["stt"]["api_key"] == vs.read_token() != "a" * 64
    assert block["stt_detects_language"] is True and block["tts_accepts_language"] is True
    assert block["tts_voices"] == ["af_heart"]
    hq_calls_settings.forget_observations()


# --- second review of #1639 ------------------------------------------------------------------


def _managed(monkeypatch, tmp_path):
    """A user service manager that does nothing (no real systemd)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AGENT_SESSIONS_NO_SERVICE", raising=False)
    monkeypatch.setattr(vs, "systemd_user", lambda: True)
    monkeypatch.setattr(vs, "_systemctl", lambda *a: None)


def test_a_failed_restart_of_the_same_set_keeps_it_and_its_unit(monkeypatch, tmp_path):
    """Re-running setup on the same release (here to switch models) whose restart fails must
    leave `current` and the unit as they were — never unlink the set being re-installed."""
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    good, unit = vs.active_set(), vs.unit_path().read_text()
    monkeypatch.setattr(vs, "restart_verified", lambda t: False)
    assert vs.setup(stt="small", yes=True, fetch=fetch, say=lambda m: None) == 2
    assert vs.active_set() == good and vs.runtime_python() is not None
    assert vs.unit_path().read_text() == unit  # still the `base` model's unit


def test_ctrl_c_mid_switch_puts_everything_back(monkeypatch, tmp_path):
    _prior_generation()
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)

    def interrupted(stt):
        raise KeyboardInterrupt

    monkeypatch.setattr(vs, "install_unit", interrupted)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    with pytest.raises(KeyboardInterrupt):
        vs.setup(yes=True, fetch=fetch, say=lambda m: None)
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}


def test_a_changed_server_script_is_a_new_generation(monkeypatch, tmp_path):
    v1, v2 = tmp_path / "v1.py", tmp_path / "v2.py"
    v1.write_text("# release 1\n")
    v2.write_text("# release 2\n")
    monkeypatch.setattr(vs, "SERVER_SCRIPT", v1)
    first, runtime = vs.lock_sha("server"), vs.lock_sha("runtime")
    monkeypatch.setattr(vs, "SERVER_SCRIPT", v2)
    assert vs.lock_sha("server") != first  # so `build` makes it, and `stale()` says so
    assert vs.lock_sha("runtime") == runtime


def test_a_call_admitted_before_any_setup_still_holds_the_lock():
    assert not vs.root().exists()
    with vs.call_lock():
        with vs.no_call() as free:
            assert not free  # a setup/rollback/rotate/remove started now must wait


def test_admission_names_an_unusable_call_lock(monkeypatch):
    @contextlib.contextmanager
    def broken():
        raise PermissionError(13, "Permission denied")
        yield

    monkeypatch.setattr(vs, "call_lock", broken)
    with pytest.raises(voice_calls.CallError) as e:
        asyncio.run(voice_calls.Registry().admit(cid="x", contact="europe-man", voice="v",
                                                operator=None, app=None))  # fmt: skip
    assert e.value.status == 503


def test_a_rollback_whose_server_fails_changes_nothing(monkeypatch, tmp_path):
    _prior_generation()
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    new, unit = vs.active_set(), vs.unit_path().read_text()
    said = []
    monkeypatch.setattr(vs, "restart_verified", lambda t: False)
    assert vs.rollback(say=said.append) == 2
    assert vs.active_set() == new and vs.unit_path().read_text() == unit
    assert "nothing was changed" in said[-1]


def test_rollback_restores_the_previous_speech_model(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(stt="small", yes=True, fetch=fetch, say=lambda m: None) == 0
    first = vs.active_set()
    monkeypatch.setattr(vs, "lock_sha", lambda k: "9" * 16)  # the next release
    assert vs.setup(stt="base", yes=True, fetch=fetch, say=lambda m: None) == 0
    assert "--stt base" in vs.unit_path().read_text()
    assert vs.rollback(say=lambda m: None) == 0
    assert vs.active_set() == first and "--stt small" in vs.unit_path().read_text()


@pytest.mark.parametrize("with_uv", [True, False])
def test_installs_are_wheels_only(monkeypatch, with_uv):
    """A source archive would be built with an unhashed backend: never."""
    seen: list[list[str]] = []

    def run(argv, log):
        seen.append(argv)
        if "venv" in argv:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(vs, "_run", run)
    monkeypatch.setattr(vs.shutil, "which", lambda n: "/usr/bin/uv" if with_uv else None)
    vs.build("runtime", log=None, python=sys.executable)
    install = next(a for a in seen if "install" in a)
    assert "--require-hashes" in install and "--no-deps" in install
    if with_uv:
        assert "--no-build" in install
    else:
        assert install[install.index("--only-binary") + 1] == ":all:"


def test_the_runtime_lock_carries_no_source_only_package():
    assert "docopt==" not in (vs.LOCKS / "runtime.txt").read_text()


def test_two_setups_never_run_at_once(monkeypatch):
    fetch = _fake_build(monkeypatch)
    said: list[str] = []
    with vs.setup_lock() as alone:
        assert alone
        assert vs.setup(yes=True, fetch=fetch, say=said.append) == 1
    assert "A `voice setup`, rollback, rotation or remove is running" in said[-1]
    assert vs.active_set() is None


def test_setup_refuses_under_a_running_unmanaged_server(monkeypatch):
    _prior_generation()
    fetch = _fake_build(monkeypatch)
    said: list[str] = []
    with vs.no_serve():
        assert vs.setup(yes=True, fetch=fetch, say=said.append) == 1
    assert "`voice serve` is running" in said[-1]
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}


# --- the local server is reached over its socket only (#1639 review) -----------------------


def test_the_local_server_is_reached_over_its_socket_never_its_tcp_port(monkeypatch):
    """Review 6895: whoever holds 127.0.0.1:<port> must never see a request — not after a proof,
    not ever. The settings' local URL is mapped to the owner-only socket."""
    import socket
    import tempfile
    import threading

    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(4)
    tcp.settimeout(0.2)
    port = tcp.getsockname()[1]
    accepted: list[bool] = []
    stop = threading.Event()

    def impostor():
        while not stop.is_set():
            try:
                conn, _ = tcp.accept()
            except OSError:
                continue
            accepted.append(True)
            conn.close()

    t = threading.Thread(target=impostor, daemon=True)
    t.start()
    d = tempfile.mkdtemp(prefix="bl-uds-")  # short: AF_UNIX paths are limited
    path = os.path.join(d, "s.sock")
    seen: list[str] = []

    async def go():
        async def serve(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            seen.append(head.decode())
            body = b'{"data": [{"id": "faster-whisper"}]}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                         b"Connection: close\r\n\r\n" + body)  # fmt: skip
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(serve, path)
        try:
            return await speech_endpoints.list_models(
                speech_endpoints.Endpoint(f"http://127.0.0.1:{port}/v1", "", TOKEN)
            )
        finally:
            server.close()

    monkeypatch.setattr(speech_endpoints, "LOCAL_SERVER_PORT", port)
    monkeypatch.setenv("AGENT_SESSIONS_SPEECH_SOCKET", path)
    try:
        assert asyncio.run(go()) == ["faster-whisper"]
    finally:
        stop.set()
        t.join(2)
        tcp.close()
        os.unlink(path) if os.path.exists(path) else None
        os.rmdir(d)
    assert accepted == []  # the TCP port was never contacted
    assert f"authorization: bearer {TOKEN}" in seen[0].lower()  # the token went over the socket


def test_with_no_socket_the_local_server_is_unreachable_not_tcp(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_SPEECH_SOCKET", "/nonexistent/bl/s.sock")
    with pytest.raises(speech_endpoints.SpeechError) as e:
        asyncio.run(speech_endpoints.list_models(
            speech_endpoints.Endpoint(vs.LOCAL_URL, "", TOKEN)))  # fmt: skip
    assert e.value.code == "unreachable"


def test_the_worker_is_told_the_socket():
    env = voice_calls._worker_env({})
    assert env["AGENT_SESSIONS_SPEECH_SOCKET"] == str(vs.socket_path())


# --- synthesis is bounded, and a stalled reader releases the model (#1639 review) -------------


def test_synthesis_output_is_capped():
    async def go():
        lock = asyncio.Lock()

        async def endless():
            while True:
                yield b"\0" * 1000
                await asyncio.sleep(0)

        got = b""
        with pytest.raises(speech_server.SynthesisLimit):  # never ended as if complete
            async for c in speech_server.bounded_stream(lock, endless, out_max=10_000, total_s=5,
                                                        stall_s=5):  # fmt: skip
                got += c
        return got, lock.locked()

    got, held = asyncio.run(go())
    assert len(got) <= 10_000 and not held


def test_a_stalled_reader_releases_the_model():
    async def go():
        lock = asyncio.Lock()

        async def chunks():
            while True:
                yield b"\0" * 100
                await asyncio.sleep(0)

        stream = speech_server.bounded_stream(lock, chunks, out_max=10**9, total_s=60, stall_s=0.2)
        await stream.__anext__()  # the reader takes one chunk, then stops reading
        await asyncio.sleep(0.6)
        free = not lock.locked()  # the producer gave the model up
        await stream.aclose()
        return free

    assert asyncio.run(go())


# --- "Use this machine" never saves a token a rotation replaced (#1639 review) ---------------


def test_a_save_carrying_a_replaced_token_is_refused():
    vs._write_token("a" * 64)
    saved: list[dict] = []
    assert vs.save_if_token_current("a" * 64, {"x": 1}, save=saved.append) is True
    vs._write_token("b" * 64)  # a rotation landed in between
    assert vs.save_if_token_current("a" * 64, {"x": 2}, save=saved.append) is False
    assert saved == [{"x": 1}]


def test_use_this_machine_refuses_when_the_token_rotates_meanwhile(
    auth_cfg, _local_server, monkeypatch
):
    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    c.post("/login", data={"username": "marcus", "password": "hunter2"}, follow_redirects=False,
           headers={"Origin": auth_cfg.origin})  # fmt: skip
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    vs._write_token(TOKEN)
    real = speech_endpoints.discover

    async def rotating_discover(ep):
        vs._write_token("r" * 64)  # `voice setup --rotate-token` completes during the check
        return await real(ep)

    monkeypatch.setattr(speech_endpoints, "discover", rotating_discover)
    r = c.post("/api/voice/local/use", headers=hdr)
    assert r.status_code == 409 and r.json()["code"] == "token_changed"
    assert hq_calls_settings.get()["stt"]["api_key"] != TOKEN


# --- review 6896 -----------------------------------------------------------------------------


def test_a_source_stalled_before_its_first_chunk_releases_the_model_at_the_deadline():
    async def go():
        lock = asyncio.Lock()

        async def stalled():
            await asyncio.sleep(3600)
            yield b""

        t0 = asyncio.get_running_loop().time()
        with pytest.raises(speech_server.SynthesisLimit):
            async for _ in speech_server.bounded_stream(lock, stalled, out_max=10, total_s=0.2,
                                                        stall_s=5):  # fmt: skip
                pass
        return asyncio.get_running_loop().time() - t0, lock.locked()

    took, held = asyncio.run(go())
    assert took < 2.0 and not held


def test_a_failing_synthesis_raises_to_the_reader():
    async def go():
        async def breaks():
            yield b"\0" * 10
            raise RuntimeError("model crashed")

        got = []
        with pytest.raises(RuntimeError):
            async for c in speech_server.bounded_stream(asyncio.Lock(), breaks):
                got.append(c)
        return got

    assert asyncio.run(go()) == [b"\0" * 10]


class _BrokenTTS(FakeTTS):
    def __init__(self, after: int):
        self.after = after

    async def stream(self, text, voice, speed, lang):
        for _ in range(self.after):
            yield b"\0\0" * 100
        raise RuntimeError("model crashed")


@pytest.mark.parametrize("fmt", ["wav", "pcm"])
def test_a_synthesis_failing_before_any_audio_is_an_error_status(fmt):
    c = TestClient(speech_server.create_app(TOKEN, FakeSTT(), _BrokenTTS(after=0)),
                   headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
    r = c.post("/v1/audio/speech", json={"input": "hi", "voice": "af_heart",
                                         "response_format": fmt})  # fmt: skip
    assert r.status_code == 502 and r.json()["error"]["code"] == "synthesis_failed"


def test_a_wav_failing_midway_is_an_error_not_partial_audio():
    c = TestClient(speech_server.create_app(TOKEN, FakeSTT(), _BrokenTTS(after=2)),
                   headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
    r = c.post("/v1/audio/speech", json={"input": "hi", "voice": "af_heart",
                                         "response_format": "wav"})  # fmt: skip
    assert r.status_code == 502


def test_a_pcm_stream_failing_midway_is_aborted_not_completed():
    c = TestClient(speech_server.create_app(TOKEN, FakeSTT(), _BrokenTTS(after=2)),
                   headers={"Authorization": f"Bearer {TOKEN}"})  # fmt: skip
    with pytest.raises(RuntimeError):  # the response is broken off, never ended cleanly
        c.post("/v1/audio/speech", json={"input": "hi", "voice": "af_heart"})


def test_an_oversized_synthesis_is_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(speech_server, "OUTPUT_MAX", 100)
    c = TestClient(_server(), headers={"Authorization": f"Bearer {TOKEN}"})
    r = c.post("/v1/audio/speech", json={"input": "hi", "voice": "af_heart",
                                         "response_format": "wav"})  # fmt: skip
    assert r.status_code == 413


def test_remove_waits_for_a_running_setup(monkeypatch):
    """Review 6896: remove inside a setup's transaction would delete what it just built."""
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    said: list[str] = []
    with vs.setup_lock() as alone:  # a setup building right now
        assert alone
        assert vs.remove(yes=True, say=said.append) == 1
    assert "A `voice setup`, rollback, rotation or remove is running" in said[-1]
    assert vs.current("runtime").exists()


def test_remove_keeps_everything_when_the_service_will_not_stop(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0

    def stuck(*a):
        if a[:1] == ("disable",):
            raise subprocess.CalledProcessError(1, "systemctl")

    monkeypatch.setattr(vs, "_systemctl", stuck)
    said: list[str] = []
    assert vs.remove(yes=True, say=said.append) == 2
    assert "Not removed" in said[-1]
    assert vs.unit_path().exists() and vs.read_token() and vs.current("runtime").exists()


def test_a_rotation_whose_settings_write_fails_late_puts_the_settings_back(monkeypatch):
    """Review 6896: the save renamed the file, then its fsync failed — the settings already
    carry the new token. Rollback must put them back to the old one, too."""
    _prior_generation()
    _local_settings()
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    calls = {"n": 0}

    def late_failing_save(patch):
        calls["n"] += 1
        hq_calls_settings.save(patch)
        if calls["n"] == 1:
            raise OSError(5, "fsync failed")

    assert vs.rotate_token(say=lambda m: None, save=late_failing_save) == 2
    assert vs.read_token() == "a" * 64
    block = hq_calls_settings.get()
    assert block["stt"]["api_key"] == block["tts"]["api_key"] == "a" * 64


def test_voice_serve_holds_its_lock_from_before_it_reads_the_token(monkeypatch):
    _prior_generation()
    srv = vs.current("server")
    (srv / "speech_server.py").write_text("")
    seen = {}

    def execve(path, argv, env):
        seen["token"] = env["SPEECH_SERVER_TOKEN"]
        with vs.no_serve() as free:  # what a rotation would see right now
            seen["rotation_could_run"] = free
        raise SystemExit(0)

    monkeypatch.setattr(vs.os, "execve", execve)
    with pytest.raises(SystemExit):
        vs.serve()
    assert seen == {"token": "a" * 64, "rotation_could_run": False}


# --- review 6897 -----------------------------------------------------------------------------


def _then_under_the_lock(monkeypatch, action):
    """Run `action` the moment a change acquires `setup.lock`: what a concurrent rotation or
    remove that finished just before would have left."""
    real = vs.setup_lock

    @contextlib.contextmanager
    def lock():
        with real() as alone:
            if alone:
                action()
            yield alone

    monkeypatch.setattr(vs, "setup_lock", lock)


def test_a_failed_rotation_rolls_back_to_the_token_current_under_its_lock(monkeypatch):
    """Rotation B finished while A waited: A's failure must restore B's token, never the one A
    saw before it held the lock."""
    _prior_generation()
    _local_settings()

    def rotation_b_landed():
        vs._write_token("b" * 64)
        hq_calls_settings.save({s: {"base_url": vs.LOCAL_URL, "api_key": "b" * 64}
                                for s in ("stt", "tts")})  # fmt: skip

    _then_under_the_lock(monkeypatch, rotation_b_landed)
    monkeypatch.setattr(vs, "restart_verified", lambda t: t == "b" * 64)  # the new one fails
    assert vs.rotate_token(say=lambda m: None) == 2
    assert vs.read_token() == "b" * 64
    assert hq_calls_settings.get()["stt"]["api_key"] == "b" * 64


def test_a_rotation_after_a_remove_recreates_nothing(monkeypatch):
    _prior_generation()
    _then_under_the_lock(monkeypatch, lambda: vs._remove(models=False))
    said: list[str] = []
    assert vs.rotate_token(say=said.append) == 1
    assert vs.read_token() is None and "isn't set up" in said[-1]


@pytest.mark.parametrize("held", ["serve", "setup"])
def test_rollback_takes_the_same_locks(monkeypatch, held):
    _prior_generation()
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    new = vs.active_set()
    lock = vs.no_serve if held == "serve" else vs.setup_lock
    with lock() as got:
        assert got
        assert vs.rollback(say=lambda m: None) == 1
    assert vs.active_set() == new


def test_remove_never_takes_an_unknown_service_state_as_stopped(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    monkeypatch.setattr(vs, "unit_active", lambda: None)  # the status query itself failed
    assert vs.remove(yes=True, say=lambda m: None) == 2
    assert vs.unit_path().exists() and vs.read_token()


def test_the_token_is_durable_before_anything_is_told(monkeypatch):
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(vs.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    vs.root().mkdir(parents=True, exist_ok=True)
    vs._write_token("c" * 64)
    assert len(synced) >= 2  # the file, then its directory
    assert vs.read_token() == "c" * 64


def test_status_names_settings_left_on_an_old_token():
    _prior_generation()
    _local_settings()  # settings carry "a"*64, the token file too
    assert vs.status()["settings_token_mismatch"] is False
    vs._write_token("d" * 64)  # a crash between rotation's two writes
    assert vs.status()["settings_token_mismatch"] is True


def test_a_cancellation_resistant_model_is_never_overlapped(monkeypatch):
    """Reviews 6897/6898: a generator that ignores cancellation (in its next chunk AND its close).
    The READER is freed at the deadline; the MODEL stays held while that work may still run — the
    next synthesis does not start on it — and the server is told to exit for a restart."""
    monkeypatch.setattr(speech_server, "CLEANUP_S", 0.2)
    stop = {"now": False}
    running = {"n": 0, "max": 0}

    async def stubborn_wait():
        while not stop["now"]:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                continue  # ignores every cancel

    async def resistant():
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        try:
            await stubborn_wait()
            yield b""
        finally:
            await stubborn_wait()  # and its close never finishes either
            running["n"] -= 1

    async def go():
        lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(speech_server.SynthesisLimit):
            async for _ in speech_server.bounded_stream(lock, resistant, out_max=10, total_s=0.2,
                                                        stall_s=5):  # fmt: skip
                pass
        took = loop.time() - t0
        # The next synthesis must NOT get the model while the first may still be working.
        second = asyncio.ensure_future(speech_server.bounded_stream(
            lock, resistant, out_max=10, total_s=5, stall_s=5).__anext__())  # fmt: skip
        await asyncio.sleep(0.5)
        overlapped = second.done() or running["max"] > 1
        second.cancel()
        stop["now"] = True  # let the abandoned work end before the loop closes
        await asyncio.sleep(0.2)
        return took, overlapped, lock.locked()

    took, overlapped, held = asyncio.run(go())
    assert took < 2.0  # the reader is not held
    assert not overlapped and held  # the model is not handed on
    assert STUCK  # and the server exits to be restarted


def test_a_cooperative_stall_releases_the_model_normally():
    async def go():
        lock = asyncio.Lock()

        async def stalled():
            await asyncio.sleep(3600)  # cancellable
            yield b""

        with pytest.raises(speech_server.SynthesisLimit):
            async for _ in speech_server.bounded_stream(lock, stalled, out_max=10, total_s=0.2,
                                                        stall_s=5):  # fmt: skip
                pass
        return lock.locked()

    assert asyncio.run(go()) is False and not STUCK


def test_closing_a_pcm_body_after_its_first_chunk_releases_the_model_at_once():
    async def go():
        lock = asyncio.Lock()

        async def chunks():
            while True:
                yield b"\0" * 100
                await asyncio.sleep(0)

        stream = speech_server.bounded_stream(lock, chunks, out_max=10**9, total_s=60, stall_s=30)
        first = await stream.__anext__()
        body = speech_server.pcm_body(first, stream)
        assert await body.__anext__() == first
        await body.aclose()  # the client went away right after the first chunk
        return lock.locked()

    assert asyncio.run(go()) is False


# --- review 6898 -----------------------------------------------------------------------------


def test_remove_refuses_when_the_service_manager_cannot_be_reached(monkeypatch, tmp_path):
    _managed(monkeypatch, tmp_path)
    fetch = _fake_build(monkeypatch)
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 0
    monkeypatch.setattr(vs, "systemd_user", lambda: False)  # the bus is gone
    said: list[str] = []
    assert vs.remove(yes=True, say=said.append) == 2
    assert vs.unit_path().exists() and vs.read_token() and "Not removed" in said[-1]


@pytest.mark.parametrize(
    "stdout,code,want",
    [(b"active\n", 0, True), (b"activating\n", 3, True), (b"inactive\n", 3, False),
     (b"failed\n", 3, False), (b"", 1, None), (b"Failed to connect to bus\n", 1, None)],
)  # fmt: skip
def test_the_service_state_is_what_systemd_reported(monkeypatch, tmp_path, stdout, code, want):
    _managed(monkeypatch, tmp_path)
    vs.unit_path().parent.mkdir(parents=True, exist_ok=True)
    vs.unit_path().write_text("[Unit]\n")

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=b"")

    monkeypatch.setattr(vs.subprocess, "run", run)
    assert vs.unit_active() is want


def test_rotation_fails_when_an_installed_service_cannot_be_restarted(monkeypatch, tmp_path):
    _prior_generation()
    _local_settings()
    _managed(monkeypatch, tmp_path)
    vs.unit_path().parent.mkdir(parents=True, exist_ok=True)
    vs.unit_path().write_text("[Unit]\n")
    monkeypatch.setattr(vs, "systemd_user", lambda: False)  # unit present, manager unreachable
    assert vs.restart_verified("x") is False
    assert vs.rotate_token(say=lambda m: None) == 2
    assert vs.read_token() == "a" * 64
    assert hq_calls_settings.get()["stt"]["api_key"] == "a" * 64


def test_a_token_write_failing_after_its_rename_is_rolled_back(monkeypatch):
    _prior_generation()
    _local_settings()
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    real_replace, real_fsync = os.replace, os.fsync
    state = {"renamed": False, "failed": False}

    def replace(a, b):
        real_replace(a, b)
        if Path(b) == vs.env_file() and not state["failed"]:
            state["renamed"] = True

    def fsync(fd):
        if state["renamed"] and not state["failed"]:
            state["failed"] = True
            raise OSError(5, "directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(vs.os, "replace", replace)
    monkeypatch.setattr(vs.os, "fsync", fsync)
    assert vs.rotate_token(say=lambda m: None) == 2
    assert vs.read_token() == "a" * 64
    assert hq_calls_settings.get()["stt"]["api_key"] == "a" * 64


def test_an_upgrade_with_an_unreachable_service_manager_switches_nothing(monkeypatch, tmp_path):
    """Review 6899: a present unit whose manager is unreachable is not "unmanaged": the upgrade
    must not switch, prune or report success while that service keeps the old set."""
    _prior_generation()
    _managed(monkeypatch, tmp_path)
    vs.unit_path().parent.mkdir(parents=True, exist_ok=True)
    vs.unit_path().write_text("[Unit]\n")
    monkeypatch.setattr(vs, "systemd_user", lambda: False)  # the user bus is gone
    restarts: list[str] = []
    monkeypatch.setattr(vs, "restart_verified", lambda t: restarts.append(t) or True)
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 2
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}  # the previous set, still
    assert (vs.root() / "sets" / ("0" * 16 + "-" + "0" * 16)).is_dir()  # nothing pruned
    assert "can't be reached" in vs.read_stamp()["last_error"] and restarts == []


def test_a_manager_dropping_after_the_preflight_puts_the_previous_set_back(monkeypatch, tmp_path):
    """Review 6900: the manager answers the preflight, then is gone at `install_unit` — the
    installed service can't be restarted onto the new set: restore, fail, never "unmanaged"."""
    _prior_generation()
    _managed(monkeypatch, tmp_path)
    vs.unit_path().parent.mkdir(parents=True, exist_ok=True)
    vs.unit_path().write_text("[Unit]\n# the installed service\n")
    answers = iter([True])  # the preflight sees it; every later check does not
    monkeypatch.setattr(vs, "systemd_user", lambda: next(answers, False))
    monkeypatch.setattr(vs, "restart_verified", lambda t: True)
    fetch = _fake_build(monkeypatch)
    assert vs.setup(yes=True, fetch=fetch, say=lambda m: None) == 2
    assert vs.active() == {k: "0" * 16 for k in vs.KINDS}
    assert vs.unit_path().read_text() == "[Unit]\n# the installed service\n"
    assert "can't be reached" in vs.read_stamp()["last_error"]
