"""`agent-sessions voice …` (#1397 P4): calls on this machine, free and offline after one download.

`setup` installs, under `$PREFIX/voice` (`PREFIX` = `AGENT_SESSIONS_HOME`):

* the call worker's **runtime** (Pipecat) and the local **speech server**, each a venv built from
  a hash-locked lock file shipped with the app (`voice_locks/*.txt`, `pip --require-hashes
  --no-deps`, so no resolver runs here). A generation lives in `<kind>/<lock-sha>/` and becomes
  active only after it verifies, by an atomic `current` symlink swap; the previous generation is
  kept for `--rollback`. A failure leaves `current`, the service and the settings untouched;
* the speech **models**, each pinned by size and sha256, downloaded to `.part` and renamed only
  once verified;
* a **token** in `server/speech.env` (0600, `SPEECH_SERVER_TOKEN=…`), read by the unit through
  `EnvironmentFile=` and by the app's "Use this machine". It is never in argv, the unit or a log;
* the `agent-sessions-speech` **user service**: loopback only, under a resource budget. Skipped
  with `AGENT_SESSIONS_NO_SERVICE=1` or without a user systemd (`voice serve` runs it instead).

Every subprocess is a literal argv list (the shell-free guarantee). `remove` refuses while a call
holds `voice/call.lock`.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8766
#: How the settings NAME this machine's speech server. It is reached only over `socket_path()`
#: (`speech_endpoints` maps this origin to the socket); nothing listens on, or is sent to, the port.
LOCAL_URL = f"http://{HOST}:{PORT}/v1"
UNIT = "agent-sessions-speech.service"
KINDS = ("runtime", "server")
LOCKS = Path(__file__).resolve().parent / "voice_locks"
SERVER_SCRIPT = Path(__file__).resolve().parent / "speech_server.py"
VOICES = {"woman": "af_heart", "man": "am_michael"}
# `base` is the CPU default: measured 1.6 s per utterance on 2 threads, vs 4.2 s for `small`
# (faster-whisper encodes a 30 s window, so the cost is per turn): docs/voice/p4-hardening.md.
STT_MODELS = ("base", "small")
FETCH_TIMEOUT_S = 60.0
# Budget of the speech service (systemd) — and of `voice serve`, in-process where it can.
THREADS = 2
MEMORY_MAX = "3G"
CPU_QUOTA = "200%"


class SetupError(RuntimeError):
    """A setup step failed; nothing active was changed."""


@dataclass(frozen=True)
class Artifact:
    path: str  # relative to models/
    url: str
    size: int
    sha256: str


_KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_HF = "https://huggingface.co/Systran/faster-whisper-{m}/resolve/{rev}/{f}"
_WHISPER_REV = {
    "small": "536b0662742c02347bc0e980a01041f333bce120",
    "base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
}
_WHISPER_FILES = {
    "small": {
        "config.json": (2370, "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"),  # noqa: E501
        "model.bin": (483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),  # noqa: E501
        "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),  # noqa: E501
        "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),  # noqa: E501
    },
    "base": {
        "config.json": (2309, "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),  # noqa: E501
        "model.bin": (145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),  # noqa: E501
        "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),  # noqa: E501
        "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),  # noqa: E501
    },
}  # fmt: skip


def models_for(stt: str) -> list[Artifact]:
    """Everything the speech server loads: Kokoro (TTS) + the chosen faster-whisper (STT)."""
    out = [
        Artifact("kokoro-v1.0.int8.onnx", f"{_KOKORO}/kokoro-v1.0.int8.onnx", 92361271,
                 "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
        Artifact("voices-v1.0.bin", f"{_KOKORO}/voices-v1.0.bin", 28214398,
                 "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
    ]  # fmt: skip
    for f, (size, sha) in _WHISPER_FILES[stt].items():
        url = _HF.format(m=stt, rev=_WHISPER_REV[stt], f=f)
        out.append(Artifact(f"faster-whisper-{stt}/{f}", url, size, sha))
    return out


# --- paths -----------------------------------------------------------------------------------


def prefix() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


def socket_path() -> Path:
    """The local speech server's Unix socket, inside the owner-only (0700) voice dir."""
    return root() / "speech.sock"


def root() -> Path:
    return prefix() / "voice"


def env_file() -> Path:
    return root() / "server" / "speech.env"


def current(kind: str) -> Path:
    """`kind`'s active generation, through the ONE activation pointer `voice/current`."""
    return root() / "current" / kind


def active_set() -> str | None:
    link = root() / "current"
    return os.readlink(link).rsplit("/", 1)[-1] if link.is_symlink() else None


def active() -> dict[str, str]:
    """The active generation of each kind, read from the pointer (the stamp only reports)."""
    out = {}
    for kind in KINDS:
        link = current(kind)
        if link.is_symlink() and (link / ".verified").exists():
            out[kind] = os.readlink(link).rsplit("/", 1)[-1]
    return out


def stamp_path() -> Path:
    return root() / "stamp.json"


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT


def lock_sha(kind: str) -> str:
    """A generation's id: its lock — and, for the server, the server script it carries, so a
    release that changes only `speech_server.py` still deploys a new generation."""
    h = hashlib.sha256((LOCKS / f"{kind}.txt").read_bytes())
    if kind == "server":
        h.update(b"\0speech_server.py\0" + SERVER_SCRIPT.read_bytes())
    return h.hexdigest()[:16]


def read_stamp() -> dict:
    try:
        data = json.loads(stamp_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_stamp(data: dict) -> None:
    tmp = stamp_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, stamp_path())


def runtime_python() -> Path | None:
    """The set-up runtime's interpreter, if a verified one is active."""
    py = current("runtime") / "bin" / "python"
    return py if py.exists() else None


def stale() -> str | None:
    """Why the active setup no longer matches this release's locks, or None."""
    for kind, gen in active().items():
        if gen != lock_sha(kind):
            return f"the {kind} was set up for another release: run `agent-sessions voice setup`"
    return None


# --- the token -------------------------------------------------------------------------------


def read_token() -> str | None:
    try:
        for line in env_file().read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "SPEECH_SERVER_TOKEN" and len(v.strip()) >= 32:
                return v.strip()
    except OSError:
        return None
    return None


def _write_token(token: str) -> None:
    path = env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"SPEECH_SERVER_TOKEN={token}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Durable BEFORE anything (the settings) is told about it: a crash can then only leave a new
    # token with old settings, which `voice status` reports and "Use this machine" repairs.
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def ensure_token() -> str:
    return read_token() or _new_token()


def _new_token() -> str:
    token = secrets.token_hex(32)
    _write_token(token)
    return token


# --- downloads -------------------------------------------------------------------------------


@contextlib.contextmanager
def _fetch(url: str) -> Iterator[Iterator[bytes]]:
    """Stream a pinned artifact. TLS verified, redirects followed (the release and model hosts
    redirect to their CDNs); the bytes are trusted only after the sha256 check."""
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=FETCH_TIMEOUT_S,
                      trust_env=False) as resp:  # fmt: skip
        if resp.status_code != 200:
            raise SetupError(f"download failed ({resp.status_code}): {url}")
        yield resp.iter_bytes(1 << 20)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(art: Artifact, models: Path, fetch: Callable = _fetch,
             say: Callable[[str], None] = print) -> Path:  # fmt: skip
    """Fetch one artifact into `models/`, verified. An existing file that matches is kept."""
    dest = models / art.path
    if dest.exists() and dest.stat().st_size == art.size and _sha256(dest) == art.sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    try:
        with fetch(art.url) as chunks, open(part, "wb") as out:
            for chunk in chunks:
                n += len(chunk)
                if n > art.size:
                    raise SetupError(f"{art.path}: larger than pinned")
                h.update(chunk)
                out.write(chunk)
        if n != art.size or h.hexdigest() != art.sha256:
            raise SetupError(f"{art.path} didn't match its checksum")
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            part.unlink()
        raise
    os.replace(part, dest)
    say(f"  verified {art.path} ({art.size / 1e6:.0f} MB)")
    return dest


# --- generations -----------------------------------------------------------------------------


def _run(argv: list[str], log) -> None:
    try:
        subprocess.run(argv, check=True, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
    except (OSError, subprocess.CalledProcessError) as e:
        raise SetupError(f"{Path(argv[0]).name} failed ({e}); see setup.log") from None


_VERIFY = {
    "runtime": "import pipecat, httpx",
    # Not only importable: the server must DECODE audio the way a transcription does. An API
    # break between faster-whisper and PyAV (`metadata_errors`, PyAV 19) imported fine and failed
    # every transcription with a 400; now it fails the setup, which keeps the previous set.
    "server": (
        "import faster_whisper, kokoro_onnx, fastapi, uvicorn, multipart, io, wave\n"
        "from faster_whisper.audio import decode_audio\n"
        "b = io.BytesIO()\n"
        "w = wave.open(b, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)\n"
        "w.writeframes(bytes(3200)); w.close(); b.seek(0)\n"
        "assert len(decode_audio(b)) == 1600, 'decode_audio returned the wrong length'\n"
    ),
}


def build(kind: str, log, python: str | None = None) -> Path:
    """Build (or reuse) this release's generation of `kind`; returns its dir. Not activated."""
    gen = root() / kind / lock_sha(kind)
    if (gen / ".verified").exists():
        return gen
    if gen.exists():
        shutil.rmtree(gen)  # an earlier, unverified attempt of OUR staging; never `current`
    gen.parent.mkdir(parents=True, exist_ok=True)
    py = python or getattr(sys, "_base_executable", None) or sys.executable
    _run([py, "-m", "venv", str(gen)], log)
    vpy = str(gen / "bin" / "python")
    lock = str(LOCKS / f"{kind}.txt")
    uv = shutil.which("uv")
    # WHEELS ONLY: building a source archive would run an unhashed, unpinned build backend
    # (pip installs it in a sub-install without --require-hashes). A package with no wheel for
    # this platform fails the setup instead.
    if uv:  # same hash-checked lock, but uv reuses its local wheel cache (much faster)
        _run([uv, "pip", "install", "--quiet", "--python", vpy, "--require-hashes", "--no-deps",
              "--no-build", "-r", lock], log)  # fmt: skip
    else:
        _run([vpy, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
              "--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", lock],
             log)  # fmt: skip
    if kind == "server":
        shutil.copyfile(SERVER_SCRIPT, gen / "speech_server.py")
    _run([vpy, "-c", _VERIFY[kind]], log)
    (gen / ".verified").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    return gen


def make_set(gens: dict[str, str]) -> str:
    """A set dir linking one generation of each kind; its name is the activation id."""
    name = "-".join(gens.get(k, "none") for k in KINDS)
    d = root() / "sets" / name
    if not d.exists():
        tmp = root() / "sets" / f".{name}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for kind, gen in gens.items():
            os.symlink(f"../../{kind}/{gen}", tmp / kind)
        os.replace(tmp, d)
    return name


def activate_set(name: str | None) -> str | None:
    """Point `voice/current` at a set (or nowhere) in ONE atomic rename; returns the set it
    replaced. Both kinds therefore always switch together."""
    link = root() / "current"
    previous = active_set()
    if name is None:
        with contextlib.suppress(FileNotFoundError):
            link.unlink()
        return previous
    tmp = root() / ".current.new"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(f"sets/{name}", tmp)
    os.replace(tmp, link)
    return previous if previous != name else None


def prune(keep_sets: set[str]) -> None:
    """Drop sets other than `keep_sets` and generations no kept set uses (only lock-sha dirs
    inside voice/<kind>; never what is active or the rollback target)."""
    keep_sets = {k for k in keep_sets if k}
    used: set[str] = set()
    sets = root() / "sets"
    for d in sets.iterdir() if sets.exists() else ():
        if d.name in keep_sets:
            used.update(os.readlink(x).rsplit("/", 1)[-1] for x in d.iterdir() if x.is_symlink())
        elif not d.name.startswith("."):
            shutil.rmtree(d)
    for kind in KINDS:
        base = root() / kind
        for d in base.iterdir() if base.exists() else ():
            if (d.is_dir() and not d.is_symlink() and d.name not in used and len(d.name) == 16
                    and all(c in "0123456789abcdef" for c in d.name)):  # fmt: skip
                shutil.rmtree(d)


# --- the service -----------------------------------------------------------------------------


def server_argv(stt: str) -> list[str]:
    cur = current("server")
    return [str(cur / "bin" / "python"), str(cur / "speech_server.py"), "--uds",
            str(socket_path()), "--models", str(root() / "models"), "--stt", stt,
            "--threads", str(THREADS)]  # fmt: skip


def unit_text(stt: str) -> str:
    return "\n".join([
        "[Unit]",
        "Description=BattleLab local speech server (calling HQ, #1397)",
        "",
        "[Service]",
        "Type=simple",
        f"EnvironmentFile={env_file()}",
        f"Environment=OMP_NUM_THREADS={THREADS}",
        "ExecStart=" + " ".join(f'"{a}"' if " " in a else a for a in server_argv(stt)),
        f"MemoryMax={MEMORY_MAX}",
        f"CPUQuota={CPU_QUOTA}",
        "Nice=10",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])  # fmt: skip


def service_skipped() -> bool:
    return os.environ.get("AGENT_SESSIONS_NO_SERVICE") == "1"


def systemd_user() -> bool:
    """Whether a user systemd is reachable here."""
    exe = shutil.which("systemctl")
    if not exe or service_skipped():
        return False
    r = subprocess.run([exe, "--user", "show-environment"], capture_output=True, check=False)  # noqa: S603
    return r.returncode == 0


def _systemctl(*args: str) -> None:
    exe = shutil.which("systemctl") or "/usr/bin/systemctl"
    subprocess.run([exe, "--user", *args], check=True, capture_output=True)  # noqa: S603


def install_unit(stt: str) -> bool:
    """Write and enable the unit; False when there is no user systemd (or it's skipped)."""
    if not systemd_user():
        return False
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(stt))
    _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    return True


def probe(token: str, wait_s: float = 60.0) -> bool:
    """Does the local server answer with `token`? (Through the speech transport, inventoried.)
    Polled while a restarted server loads its models."""
    import asyncio

    from . import speech_endpoints

    async def once() -> bool:
        try:
            await speech_endpoints.list_models(speech_endpoints.Endpoint(LOCAL_URL, "", token))
        except speech_endpoints.SpeechError:
            return False
        return True

    deadline = time.monotonic() + wait_s
    while True:
        if asyncio.run(once()):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(1.0)


def restart_verified(token: str | None) -> bool | None:
    """Restart the managed service and wait for it to answer; None when nothing is managed (no
    unit). A unit whose service manager can't be reached is NOT "nothing managed": False."""
    if not unit_path().exists():
        return None
    if not systemd_user():
        return False  # an installed service we can't restart keeps whatever it was started with
    try:
        _systemctl("restart", UNIT)
    except subprocess.CalledProcessError:
        return False
    return bool(token) and probe(token)


_STOPPED = ("inactive", "failed")
_RUNNING = ("active", "activating", "deactivating", "reloading", "refreshing")


def unit_active() -> bool | None:
    """True/False only from a state systemd actually REPORTED (`is-active` prints it); None when
    there's no unit, no reachable user systemd, or a query that failed — never "stopped"."""
    if not unit_path().exists() or not systemd_user():
        return None
    exe = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        r = subprocess.run(  # noqa: S603 - literal argv
            [exe, "--user", "is-active", UNIT], capture_output=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    if state[0] in _RUNNING:
        return True
    return False if state[0] in _STOPPED else None


# --- the call lock ---------------------------------------------------------------------------


@contextlib.contextmanager
def call_lock() -> Iterator[None]:
    """Held (shared) for a call's lifetime, so a setup, rollback, rotation or `remove` (exclusive)
    can't run under a call — also one admitted BEFORE anything was set up here (the dir is made
    for the lock). Raises BlockingIOError while one of them holds it; OSError if it can't exist."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / "call.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def no_call() -> Iterator[bool]:
    """Exclusive for the duration of a change: yields False (and holds nothing) if a call runs."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / "call.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextlib.contextmanager
def token_lock() -> Iterator[None]:
    """Held while the token changes or a save writes it into the settings: a rotation and a
    "Use this machine" can never interleave (the second would save a stale token)."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / "token.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_if_token_current(token: str, patch: dict, save: Callable | None = None) -> bool:
    """Save `patch` (carrying `token`) only if `token` is still THE token, under `token_lock`."""
    from . import hq_calls_settings as hs

    with token_lock():
        if read_token() != token:
            return False
        (save or hs.save)(patch)
        return True


@contextlib.contextmanager
def setup_lock() -> Iterator[bool]:
    """One setup at a time: yields False (holding nothing) while another one runs."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / "setup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _serve_lock_fd() -> int | None:
    """`voice/serve.lock`, exclusively, or None while someone holds it."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root() / "serve.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


@contextlib.contextmanager
def no_serve() -> Iterator[bool]:
    """A foreground `voice serve` holds `voice/serve.lock` for its whole life, taken BEFORE it
    reads the token. Yields False while one does; otherwise holds the lock, so none can start (and
    read a token about to change, or run files about to swap) until this change is done."""
    fd = _serve_lock_fd()
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)


@contextlib.contextmanager
def changing(say: Callable[[str], None]) -> Iterator[bool]:
    """Every command that changes the installation (setup, rollback, rotation, remove) takes the
    SAME locks in the SAME order — setup, then serve — and reads what it acts on only once it
    holds them. Yields False (after saying why) when it must not run."""
    with setup_lock() as alone, no_serve() as no_server:
        if not alone:
            say("A `voice setup`, rollback, rotation or remove is running here: let it finish. "
                "Nothing was changed.")  # fmt: skip
            yield False
        elif not no_server:
            say("`voice serve` is running: stop it first. Nothing was changed.")
            yield False
        else:
            yield True


def _read_unit() -> str | None:
    try:
        return unit_path().read_text()
    except OSError:
        return None


def _restore_unit(text: str | None) -> None:
    """Put the unit back as it was before a failed change (or remove one it created)."""
    if not systemd_user():
        return
    path = unit_path()
    with contextlib.suppress(OSError):
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)
    with contextlib.suppress(subprocess.CalledProcessError, OSError):
        _systemctl("daemon-reload")


# --- commands --------------------------------------------------------------------------------


def _confirm(question: str, yes: bool, ask: Callable[[str], str]) -> bool:
    if yes:
        return True
    try:
        return ask(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def setup(*, stt: str = "base", runtime: bool = True, server: bool = True, yes: bool = False,
          say: Callable[[str], None] = print, ask: Callable[[str], str] = input,
          fetch: Callable = _fetch) -> int:  # fmt: skip
    if stt not in STT_MODELS:
        raise SetupError(f"--stt must be one of {STT_MODELS}")
    arts = models_for(stt) if server else []
    mb = sum(a.size for a in arts) / 1e6 + (150 if runtime else 0) + (250 if server else 0)
    say(f"Calling HQ on this machine: about {mb:.0f} MB to download into {root()}.")
    if server:
        say(f"  speech recognition faster-whisper-{stt}, voice Kokoro v1.0 (checksum-pinned)")
    if runtime:
        say("  the call runtime (Pipecat 1.13, hash-locked)")
    if not _confirm("Download and install?", yes, ask):
        say("Nothing was changed.")
        return 1
    root().mkdir(parents=True, exist_ok=True)
    os.chmod(root(), 0o700)
    with changing(say) as ok:
        if not ok:
            return 1
        return _setup_locked(stt=stt, runtime=runtime, server=server, arts=arts, say=say,
                             fetch=fetch)  # fmt: skip


def _setup_locked(*, stt: str, runtime: bool, server: bool, arts: list, say, fetch) -> int:
    st = read_stamp()

    def failed(why: str) -> int:
        st["last_error"] = why
        _write_stamp(st)
        say(f"Setup failed: {why}. Nothing active was changed.")
        return 2

    try:
        with open(root() / "setup.log", "w") as log:
            for a in arts:
                download(a, root() / "models", fetch=fetch, say=say)
            built = {
                k: build(k, log).name for k in KINDS if (runtime if k == "runtime" else server)
            }
    except SetupError as e:
        return failed(str(e))
    except OSError as e:  # a missing file, a full disk: named, never a traceback
        return failed(f"{type(e).__name__}: {e.strerror or e}")
    token = ensure_token() if server else read_token()
    gens = {**active(), **built}
    with no_call() as free:
        if not free:
            # Never under a live call: no swap, no restart, no prune. The verified generations
            # stay and the next run activates them at once.
            return failed("a call is running; end it and run setup again (no new download)")
        if unit_path().exists() and not systemd_user():
            # An INSTALLED service whose manager can't be reached can't be restarted onto the new
            # set (or verified): it would keep serving the old one while setup said "done".
            return failed("the speech service is installed but the user service manager can't "
                          "be reached to restart it; nothing was switched (no new download "
                          "next time)")  # fmt: skip
        new_set = make_set(gens)
        was_active, was_unit = active_set(), _read_unit()

        def put_back() -> None:
            # Exactly what was there: the previous set (or none — never unlinking a set that IS
            # the one being re-installed) and the previous unit, restarted.
            if was_active != new_set:
                activate_set(was_active)
            _restore_unit(was_unit)
            restart_verified(token)

        try:
            old_set = activate_set(new_set)  # the ONE switch: both kinds change together
            if server:
                managed = install_unit(stt)
                if not managed and was_unit is not None:
                    # A service IS installed, but its manager went away after the preflight: it
                    # can't be restarted onto this set. Never the unmanaged path for it.
                    put_back()
                    return failed("the user service manager can't be reached to restart the "
                                  "speech service; the previous setup is back")  # fmt: skip
                if managed and not restart_verified(token):
                    put_back()
                    return failed("the new speech server did not start; the previous setup is "
                                  "back")  # fmt: skip
                if not managed:
                    say("No user service manager here: start it with `agent-sessions voice "
                        "serve`.")  # fmt: skip
                else:
                    say(f"The speech service is running on {socket_path()} ({UNIT}).")
        except BaseException:  # an error or Ctrl-C mid-switch: never a half-switched setup
            put_back()
            raise
        prune({new_set, old_set or st.get("previous_set") or ""})
    stt_by_set = dict(st.get("stt_by_set") or {})
    if server:
        stt_by_set[new_set] = stt
    st.update(active=gens, set=new_set, previous_set=old_set or st.get("previous_set"),
              stt=stt if server else st.get("stt", stt), last_error=None, stt_by_set=stt_by_set,
              setup_at=time.strftime("%Y-%m-%dT%H:%M:%S"))  # fmt: skip
    _write_stamp(st)
    say("Done. In Settings → HQ calls, press “Use this machine”.")
    return 0


def rollback(say: Callable[[str], None] = print) -> int:
    if not root().exists():
        say("There is no previous setup to go back to.")
        return 1
    with changing(say) as ok:
        if not ok:
            return 1
        return _rollback_locked(say)


def _rollback_locked(say: Callable[[str], None]) -> int:
    st = read_stamp()  # read under the locks: what a concurrent change left is what we act on
    prev = st.get("previous_set")
    if not prev or not (root() / "sets" / prev).is_dir():
        say("There is no previous setup to go back to.")
        return 1
    with no_call() as free:
        if not free:
            say("A call is running: end it first. Nothing was changed.")
            return 1
        token, was_unit, cur = read_token(), _read_unit(), active_set()
        replaced = activate_set(prev)
        prev_stt = (st.get("stt_by_set") or {}).get(prev)
        try:
            if prev_stt in STT_MODELS and unit_path().exists():
                install_unit(prev_stt)  # the previous setup's speech model, too
            ok = restart_verified(token) is not False  # None: nothing managed here
        except BaseException:
            ok = None
            raise
        finally:
            if ok is not True:
                activate_set(cur)
                _restore_unit(was_unit)
                if ok is False:
                    restart_verified(token)
        if not ok:
            say("The previous setup's speech server did not start: nothing was changed.")
            return 2
    st.update(set=prev, previous_set=replaced, active=active(),
              stt=prev_stt if prev_stt in STT_MODELS else st.get("stt"))  # fmt: skip
    _write_stamp(st)
    say("Rolled back to the previous setup.")
    return 0


def rotate_token(say: Callable[[str], None] = print, save: Callable | None = None) -> int:
    """A new token, as one recoverable step: refused during a call (a worker would keep the old
    one); the service must answer with the new token, and the saved local endpoints must take it
    — otherwise the old token is put back (file, service, settings) and nothing changed."""
    from . import hq_calls_settings as hs

    save = save or hs.save
    if not root().exists():
        say("The speech server isn't set up.")
        return 1
    # A running `voice serve` can't be restarted from here (it would keep the old token): the
    # serve lock refuses it. The token to roll back to is read only UNDER the locks, so a
    # rotation or remove that finished meanwhile is never undone.
    with changing(say) as ok:
        if not ok:
            return 1
        old = read_token()
        if old is None or not (current("server") / "speech_server.py").exists():
            say("The speech server isn't set up.")
            return 1
        return _rotate_locked(old, save, say)


def _rotate_locked(old: str, save: Callable, say: Callable[[str], None]) -> int:
    from . import hq_calls_settings as hs

    with no_call() as free:
        if not free:
            say("A call is running: end it first. Nothing was changed.")
            return 1
        new = secrets.token_hex(32)
        # Until the settings carry the new token (or the old one is back): a "Use this machine"
        # in between would otherwise save the token that is being replaced.
        with token_lock():
            local = [s for s in ("stt", "tts") if hs.get()[s]["base_url"] == LOCAL_URL]
            try:
                _write_token(new)  # inside the rollback: a failure even after its rename is undone
                if restart_verified(new) is False:
                    raise SetupError("the speech service didn't come back with the new token")
                if local:
                    block = hs.get()
                    claims: dict = {}
                    for s in local:  # the same server, now answering with `new`: claims stay
                        claims.update(hs.rotated_claims(s, block, new))
                    save({**{s: {"base_url": LOCAL_URL, "api_key": new} for s in local},
                          **claims})  # fmt: skip
            except (SetupError, hs.SettingsError, OSError) as e:
                try:
                    _write_token(old)
                except OSError:
                    say(f"Rotation failed ({e}) and the old token could not be written back: run "
                        "`agent-sessions voice setup --rotate-token` again.")  # fmt: skip
                    return 2
                # The save may have landed before it failed (e.g. a failed fsync after the
                # rename): the settings must carry the old token again, too.
                put_back = _settings_back_to(old, local, save)
                restart_verified(old)
                if put_back:
                    say(f"Rotation failed ({e}); the old token is still in use.")
                else:
                    say(f"Rotation failed ({e}) and HQ calls' settings could not be put back: run "
                        "`agent-sessions voice setup --rotate-token` again.")  # fmt: skip
                return 2
    say("A new token is in use" + (" by HQ calls." if local else "."))
    return 0


def _settings_back_to(token: str, local: list[str], save: Callable) -> bool:
    """After a failed rotation: the local endpoints carry `token` again. True when they do."""
    from . import hq_calls_settings as hs

    try:
        cur = hs.get()
        if all(cur[s]["api_key"] == token for s in local):
            return True
        claims: dict = {}
        for s in local:
            claims.update(hs.rotated_claims(s, cur, token))
        save({**{s: {"base_url": LOCAL_URL, "api_key": token} for s in local}, **claims})
        return all(hs.get()[s]["api_key"] == token for s in local)
    except Exception:  # noqa: BLE001 — reported to the operator, never a traceback
        return False


def remove(*, models: bool = False, yes: bool = False, say: Callable[[str], None] = print,
           ask: Callable[[str], str] = input) -> int:  # fmt: skip
    if not root().exists():
        say("Calls were never set up here.")
        return 0
    what = "the call runtime, the speech server and its token" + (
        " and the models" if models else ""
    )
    if not _confirm(f"Remove {what} from {root()}?", yes, ask):
        say("Nothing was changed.")
        return 1
    # Inside a setup's transaction, never beside it (it would delete what that setup just built),
    # and never under a running `voice serve` or a call.
    with changing(say) as ok:
        if not ok:
            return 1
        with no_call() as free:
            if not free:
                say("A call is running: end it first. Nothing was changed.")
                return 1
            try:
                _remove(models)
            except SetupError as e:
                say(f"Not removed: {e}.")
                return 2
    say("Removed. Settings → HQ calls still point where they did; change them there.")
    return 0


def _remove(models: bool) -> None:
    if unit_path().exists():
        # The service must be CONFIRMED stopped before its files and token go: a server still
        # running would keep the old token. Nothing is deleted otherwise — also not when the
        # user service manager can't be reached to stop or ask it.
        if not systemd_user():
            raise SetupError("the user service manager can't be reached to stop the speech "
                             "service")  # fmt: skip
        try:
            _systemctl("disable", "--now", UNIT)
        except (subprocess.CalledProcessError, OSError) as e:
            raise SetupError(f"the speech service could not be stopped ({e})") from None
        if unit_active() is not False:  # None = unknown: never taken as stopped
            raise SetupError("the speech service could not be confirmed stopped")
        unit_path().unlink()
    activate_set(None)
    shutil.rmtree(root() / "sets", ignore_errors=True)
    for kind in KINDS:
        shutil.rmtree(root() / kind, ignore_errors=True)
    if models:
        shutil.rmtree(root() / "models", ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        stamp_path().unlink()


def serve() -> int:
    """Run the speech server in the foreground (no user systemd). Never returns on success.

    Holds `voice/serve.lock` for the server's whole life (the fd survives the exec), taken BEFORE
    the token is read: a rotation, setup or remove either sees it and refuses, or holds it and this
    waits for nothing — it refuses, and the token it would read can't change underneath it."""
    fd = _serve_lock_fd()
    if fd is None:
        print("A `voice serve` is already running, or a setup, rotation or remove is in progress: "
              "try again.", file=sys.stderr)  # fmt: skip
        return 1
    token = read_token()
    if not token or not (current("server") / "speech_server.py").exists():
        os.close(fd)
        print("The speech server isn't set up: run `agent-sessions voice setup`.", file=sys.stderr)
        return 2
    os.set_inheritable(fd, True)  # held by the server process until it exits
    argv = server_argv(read_stamp().get("stt", "base"))
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
    env.update(SPEECH_SERVER_TOKEN=token, OMP_NUM_THREADS=str(THREADS))
    print("Soft limits only (2 threads, one request at a time, nice 10): no memory or CPU cap "
          "without the user service.", file=sys.stderr)  # fmt: skip
    with contextlib.suppress(OSError):
        os.nice(10)
    os.execve(argv[0], argv, env)  # noqa: S606 - literal argv, no shell
    return 0  # pragma: no cover


def status() -> dict:
    """What Settings and `voice status` show (no network)."""
    from . import hq_calls_settings as hs

    st = read_stamp()
    installed = {k: (current(k) / ".verified").exists() for k in KINDS}
    token = read_token()
    try:
        block = hs.get()
        local = [s for s in ("stt", "tts") if block[s]["base_url"] == LOCAL_URL]
        # A crash between a rotation's two durable writes can leave settings on the OLD token:
        # named here, and "Use this machine" repairs it.
        mismatch = bool(token and local and any(block[s]["api_key"] != token for s in local))
    except Exception:  # noqa: BLE001 — status never fails on the settings store
        mismatch = None
    return {
        "installed": installed,
        "token": token is not None,
        "settings_token_mismatch": mismatch,
        "unit": unit_active(),
        "systemd": systemd_user(),
        "stt": st.get("stt"),
        "last_error": st.get("last_error"),
        "stale": stale(),
        "url": LOCAL_URL,
        # Hard caps come only from the unit; `voice serve` has soft limits (2 threads, one request
        # at a time per model, nice 10) and no memory or CPU cap.
        "limits": "hard" if unit_path().exists() and systemd_user() else "soft",
    }
