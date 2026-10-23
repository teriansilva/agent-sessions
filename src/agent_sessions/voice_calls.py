"""Calling HQ (#1397 P1): the call registry and the broker between the browser and the worker.

```
browser ──/ws/voice/{id}──▶ Call (this module, in the app) ──UDS ws──▶ voice worker (Pipecat)
   mic PCM16 16 kHz  ─────────────────────────────────────────────────▶  VAD + Smart Turn → STT
   speaker PCM16 24 kHz ◀─────────────────────────────────────────────  TTS ◀── "say"
           utterance ──▶ HQ turn (ask_orchestrator, via="call") ──▶ stored answer ──▶ say
```

The worker is a **speech terminal**: it hears (VAD, end-of-turn, STT) and speaks (TTS) and knows
nothing about HQ. Everything HQ does happens HERE, in the app, as an ordinary HQ turn with the
operator taken from the socket that opened the call — so a call reaches exactly what typing in HQ
reaches, through the same tools, limits and confirm routes (no new authority).

What HQ says is HQ's STORED text: the answer as `ask_store.append` kept it (redacted), plus fixed
sentences rendered here for cards and questions. The model never speaks unstored prose, and raw
tool output never reaches the TTS endpoint.

Limits: one call per instance; MAX_CALL_S; IDLE_S without speech either way; bounded frames and
queues. On transport loss the call ends: mic and playback stop on the client, nothing reconnects
without a new tap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import ask_orchestrator, ask_store, hq_calls_settings, hq_contacts

log = logging.getLogger(__name__)

MAX_CALL_S = 60 * 60
IDLE_S = 5 * 60
#: A client audio frame: 20–100 ms of 16 kHz PCM16 is 640–3200 bytes; anything past this is refused.
MAX_AUDIO_FRAME = 64 * 1024
MAX_CONTROL_FRAME = 8 * 1024
#: Ingress budget per second of wall time (16 kHz PCM16 = 32 KB/s; generous headroom).
MAX_INGRESS_BPS = 128 * 1024
IN_RATE = 16_000
OUT_RATE = 24_000
WORKER_START_S = 30.0
#: A call that no socket of its session joins within this ends ("not_connected").
DIAL_TIMEOUT_S = 30.0
#: Say something if HQ has not answered by then, so silence never reads as a dropped call.
ACK_AFTER_S = 1.2

#: Fixed acknowledgements while HQ works. Not model output: picked in rotation.
ACKS = (
    "One moment, Commander.",
    "Checking now, Commander.",
    "Stand by, Commander.",
)
PROGRESS_LINES = {
    "find_sessions": "Looking through your sessions.",
    "list_sessions": "Looking through your sessions.",
    "session_status": "Checking that session.",
    "session_brief": "Checking that session.",
    "list_missions": "Checking your missions.",
    "mission_status": "Checking that mission.",
    "fleet_status": "Checking the fleet.",
    "agent_usage": "Checking your usage.",
}


#: How long ending a call waits on each socket send before giving up on it.
SEND_BUDGET_S = 2.0
#: How long the "can the worker's interpreter import Pipecat?" probe may take.
RUNTIME_PROBE_S = 30.0


class CallError(Exception):
    def __init__(self, status: int, detail: str, code: str = "refused"):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


# Spoken-text helpers live in `voice_text` (no app imports: the worker uses them too).
from .voice_text import (  # noqa: E402,F401
    CHUNK_WORDS,
    card_sentences,
    pick_option,
    speakable,
    split_speech,
)

# --- the call ----------------------------------------------------------------------------------


@dataclass
class Call:
    id: str
    cid: str
    contact: str
    voice: str
    operator: object
    app: object
    registry: object
    #: The CSRF token of the session that admitted the call. The call's socket must present it
    #: (and the same session's cookie): a call's audio belongs to the session that started it.
    session_csrf: str = ""
    dial_timer: asyncio.Task | None = None
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    state: str = "dialing"  # dialing | listening | thinking | speaking | ended
    ended_reason: str = ""
    muted: bool = False
    #: The latest utterance heard while HQ was still answering the previous one.
    queued: str | None = None
    #: Bumped by every new utterance: an answer from an older generation is not spoken.
    generation: int = 0
    turn_task: asyncio.Task | None = None
    ask: dict | None = None
    _to_client: Callable[[dict | bytes], Awaitable[None]] | None = None
    _to_worker: Callable[[dict | bytes], Awaitable[None]] | None = None
    _ack_i: int = 0
    _ended: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set once nothing of this call runs any more (its worker stopped, or it never had one): only
    #: then may the registry admit the next call.
    torn_down: asyncio.Event = field(default_factory=asyncio.Event)
    worker_started: bool = False
    #: "brief" | "detailed" — Settings → HQ calls → answers (picks the registered call prompt).
    answers: str = "brief"

    def public(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.cid,
            "contact": self.contact,
            "state": self.state,
            "muted": self.muted,
            "elapsed_s": int(time.monotonic() - self.started),
            "ended_reason": self.ended_reason,
        }

    # -- wiring ---------------------------------------------------------------------------------

    async def client(self, msg: dict | bytes) -> None:
        if self._to_client is not None:
            with contextlib.suppress(Exception):
                await self._to_client(msg)

    async def worker(self, msg: dict | bytes) -> None:
        if self._to_worker is not None:
            with contextlib.suppress(Exception):
                await self._to_worker(msg)

    async def set_state(self, state: str) -> None:
        if self.state == "ended" or self.state == state:
            return
        self.state = state
        await self.client({"t": "state", "state": state})

    async def say(self, text: str, *, kind: str = "answer", turn_id: str | None = None) -> None:
        text = text.strip()
        if not text or self.state == "ended":
            return
        sid = uuid.uuid4().hex[:12]
        await self.client({"t": "caption", "who": "hq", "id": sid, "text": text, "kind": kind,
                           "turn_id": turn_id})  # fmt: skip
        await self.worker({"t": "say", "id": sid, "text": text})

    # -- events from the worker -----------------------------------------------------------------

    async def on_worker_event(self, ev: dict) -> None:
        t = ev.get("t")
        self.last_activity = time.monotonic()
        if t == "ready":
            await self.set_state("listening")
            await self.client({"t": "connected"})
            await self.greet()
        elif t == "user_started":
            await self.client({"t": "user", "speaking": True})
        elif t == "user_stopped":
            await self.client({"t": "user", "speaking": False})
        elif t == "interrupt":
            # Barge-in: the client drops what it has queued, at once.
            await self.client({"t": "interrupt"})
            if self.state == "speaking":
                await self.set_state("listening")
        elif t == "bot_started":
            await self.set_state("speaking")
            await self.client({"t": "speaking", "id": ev.get("id"), "on": True})
        elif t == "bot_stopped":
            await self.client({"t": "speaking", "id": ev.get("id"), "on": False})
            if self.state == "speaking":
                await self.set_state("thinking" if self._turn_running() else "listening")
        elif t == "utterance":
            text = str(ev.get("text") or "").strip()[: ask_orchestrator.QUERY_MAX]
            if text:
                await self.on_utterance(text)
        elif t == "error":
            await self.end(
                f"{ev.get('endpoint') or 'speech'}_failed",
                detail=str(ev.get("detail") or "the speech endpoint stopped answering")[:200],
                endpoint=str(ev.get("endpoint") or ""),
            )

    def _turn_running(self) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    async def greet(self) -> None:
        name = ""
        with contextlib.suppress(KeyError):
            name = hq_contacts.get(self.contact).name.split()[-1]
        who = f"General {name} " if name else ""
        await self.say(f"HQ, {who}speaking. Go ahead, Commander.", kind="greeting")

    async def on_utterance(self, text: str) -> None:
        if self.muted:
            return
        self.generation += 1
        await self.client({"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text})
        if self._turn_running():
            # HQ is still answering the previous question: keep only the newest one and ask it
            # when the running turn settles; that turn's answer is no longer spoken.
            self.queued = text
            return
        self.turn_task = asyncio.create_task(self._ask(text, self.generation))

    async def _ask(self, text: str, gen: int) -> None:
        await self.set_state("thinking")
        option = None
        answers = None
        if self.ask:
            idx = pick_option(text, self.ask.get("options") or [])
            if idx is not None:
                option, answers, text = idx, self.ask["question_id"], ""
            elif self.ask.get("allow_text"):
                answers = self.ask["question_id"]
            self.ask = None
        turn_id = str(uuid.uuid4())
        ack_task = asyncio.create_task(self._ack_later(gen))
        try:
            view = await self._run_turn(turn_id, text, answers, option)
        except ask_orchestrator.TurnError as e:
            view = {"status": "failed", "detail": e.detail}
        except ask_store.StoreRefused:
            view = {"status": "failed", "detail": "HQ could not store this question"}
        except Exception:  # noqa: BLE001 — never a call stuck in "thinking" with a lost queue
            log.warning("voice call turn failed", exc_info=True)
            view = {"status": "failed", "detail": ""}
        finally:
            ack_task.cancel()
        if self.state == "ended":
            return
        await self.client({"t": "turn", "turn": view})
        if gen != self.generation:
            pass  # a newer question superseded this one: its answer is on screen, not spoken
        elif view.get("status") == "done":
            ask = view.get("ask")
            self.ask = ask if isinstance(ask, dict) and ask.get("question_id") else None
            words = speakable(view.get("answer") or "")
            for line in [words, *card_sentences(view)]:
                await self.say(line, turn_id=view.get("turn_id"))
        else:
            await self.say(
                "Sorry, Commander, I couldn't answer that. "
                + (str(view.get("detail") or "")[:160]),
                kind="error",
            )
        nxt, self.queued = self.queued, None
        if nxt and self.state != "ended":
            self.turn_task = asyncio.create_task(self._ask(nxt, self.generation))
        elif self.state == "thinking":
            await self.set_state("listening")

    async def _ack_later(self, gen: int) -> None:
        await asyncio.sleep(ACK_AFTER_S)
        if gen == self.generation and self.state == "thinking":
            line = ACKS[self._ack_i % len(ACKS)]
            self._ack_i += 1
            await self.say(line, kind="ack")

    async def _run_turn(self, turn_id: str, text: str, answers, option) -> dict:
        from . import actuator  # the same snapshot the HQ route takes

        task, view = await ask_orchestrator.begin(
            self.cid,
            turn_id,
            text,
            working_keys=actuator.working_keys(self.registry),
            busy_keys=actuator.busy_keys(self.registry),
            answers=answers,
            option=option,
            contact=self.contact,
            via="call",
            call_answers=self.answers,
            operator=self.operator,
            app=self.app,
        )
        if task is None:
            return view
        queue = ask_orchestrator.listen(self.cid, task)
        said: set[str] = set()
        try:
            while not task.done():
                get = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    ev = get.result()
                    step = ev.get("tool") or ev.get("step")
                    line = PROGRESS_LINES.get(str(step))
                    if line and line not in said and self.state == "thinking":
                        said.add(line)
                        await self.say(line, kind="progress")
                    await self.client({"t": "progress", "step": str(step)[:40]})
                else:
                    get.cancel()
            return await asyncio.shield(task)
        finally:
            ask_orchestrator.unlisten(self.cid, queue)

    # -- ending ---------------------------------------------------------------------------------

    async def end(self, reason: str, *, detail: str = "", endpoint: str = "") -> None:
        if self.state == "ended":
            return
        self.state = "ended"
        self.ended_reason = reason
        try:
            # Bounded: a stalled client or worker socket must not keep a hang-up from finishing.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.client({"t": "ended", "reason": reason, "detail": detail,
                                 "endpoint": endpoint}), SEND_BUDGET_S)  # fmt: skip
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.worker({"t": "stop"}), SEND_BUDGET_S)
        finally:
            self._ended.set()
            if not self.worker_started:
                self.torn_down.set()

    def mark_torn_down(self) -> None:
        """The worker has been stopped: the next call may be admitted."""
        self.torn_down.set()

    async def wait_ended(self) -> None:
        await self._ended.wait()


async def end_if_never_joined(call: Call) -> None:
    await asyncio.sleep(DIAL_TIMEOUT_S)
    if call.state == "dialing" and call._to_client is None:
        await call.end("not_connected")


# --- the registry ------------------------------------------------------------------------------


class Registry:
    """At most one call per instance (#1397 limits)."""

    def __init__(self) -> None:
        self.current: Call | None = None
        self._lock = asyncio.Lock()

    async def admit(self, **kw) -> Call:
        async with self._lock:
            cur = self.current
            if cur is not None and cur.state != "ended":
                raise CallError(409, "a call is already in progress — end it first", "busy")
            if cur is not None and not cur.torn_down.is_set():
                raise CallError(409, "the last call is still hanging up — try again", "busy")
            call = Call(id=secrets.token_urlsafe(16), **kw)
            self.current = call
            return call

    def get(self, call_id: str) -> Call | None:
        c = self.current
        if (
            c is not None
            and isinstance(call_id, str)
            and call_id.isascii()
            and secrets.compare_digest(c.id, call_id)
        ):
            return c
        return None

    async def end_all(self, reason: str) -> None:
        if self.current is not None:
            await self.current.end(reason)


REGISTRY = Registry()


# --- the worker process ------------------------------------------------------------------------


def worker_python() -> str:
    """The interpreter that runs the worker: `AGENT_SESSIONS_VOICE_PYTHON` (a separate
    environment with Pipecat, kept across releases — docs/voice/runtime.md), else the app's own
    (installed with the `voice` extra)."""
    return os.environ.get("AGENT_SESSIONS_VOICE_PYTHON") or sys.executable


_PACKAGE_DIR = Path(__file__).resolve().parent


def _shim(d: Path) -> str:
    """A dir whose `agent_sessions` is THIS running release's package, for the worker's
    PYTHONPATH: a separate voice environment has Pipecat but not the app, and must run the
    current code after every upgrade."""
    link = d / "agent_sessions"
    if not link.exists():
        os.symlink(_PACKAGE_DIR, link)
    return str(d)


_RUNTIME_CACHE: dict[str, tuple[float, bool, str]] = {}


async def runtime_ready() -> tuple[bool, str]:
    """Whether the worker's interpreter can import Pipecat (cached for a minute)."""
    py = worker_python()
    hit = _RUNTIME_CACHE.get(py)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1], hit[2]
    try:
        with tempfile.TemporaryDirectory(prefix="bl-voice-check-") as d:
            proc = await asyncio.create_subprocess_exec(
                py, "-c", "import pipecat, agent_sessions.voice_worker",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=_worker_env({}, shim=_shim(Path(d))), cwd=d,
            )  # fmt: skip
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=RUNTIME_PROBE_S)
            except BaseException:  # a timeout, or the asking request cancelled: never leave it
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.shield(proc.wait())
                raise
        ok = proc.returncode == 0
        why = "" if ok else (err.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1][:200]
    except (OSError, TimeoutError) as e:
        ok, why = False, str(e)[:200]
    if not ok:
        why = f"the voice runtime is not installed ({why})"
    _RUNTIME_CACHE[py] = (time.monotonic(), ok, why)
    return ok, why


def _worker_env(tokens: dict[str, str], shim: str | None = None) -> dict[str, str]:
    """A sanitized environment: no app secrets, no agent credentials — only what Python needs to
    start, plus the two speech tokens."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    if shim is not None:
        env["PYTHONPATH"] = shim  # this release's package, ahead of anything inherited
    # A worker is one call: keep its native thread pools small (#1362's budget spirit).
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        env[k] = "2"
    env.update({k: v for k, v in tokens.items() if v})
    return env


@dataclass
class Worker:
    proc: asyncio.subprocess.Process
    sock: Path
    dir: Path

    async def stop(self) -> None:
        """TERM, then KILL, the worker's whole process group (it was started in a session of
        its own, so the group id is its pid): children it forked go with it."""
        pgid = self.proc.pid
        if pgid is None or pgid <= 1:  # killpg(1) would be kill(-1): every process we own
            return
        if self.proc.returncode is not None and not _group_alive(pgid):
            # Already gone (a normal hang-up exits by itself): never signal a number that may
            # have been reused by another process group.
            if not os.environ.get("VOICE_KEEP_WORKER_DIR"):
                shutil.rmtree(self.dir, ignore_errors=True)
            return
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig)
            try:
                if self.proc.returncode is None:
                    await asyncio.wait_for(self.proc.wait(), timeout=wait)
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.2)
                if not _group_alive(pgid):
                    break
            except TimeoutError:
                continue
        if not os.environ.get("VOICE_KEEP_WORKER_DIR"):
            shutil.rmtree(self.dir, ignore_errors=True)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def start_worker(settings: dict) -> Worker:
    """Spawn the worker on a fresh private socket. The tokens ride the environment, never argv
    (argv is world-readable in /proc on a shared host)."""
    d = Path(tempfile.mkdtemp(prefix="bl-voice-"))
    os.chmod(d, 0o700)
    sock = d / "w.sock"
    tokens = {
        "VOICE_STT_TOKEN": settings["stt"]["api_key"],
        "VOICE_TTS_TOKEN": settings["tts"]["api_key"],
    }
    # stderr goes to a file in the private dir, never an unread pipe: a chatty worker would
    # fill a pipe's buffer and block its own event loop mid-call.
    log_path = d / "worker.log"
    try:
        with open(log_path, "wb") as log_f:
            proc = await asyncio.create_subprocess_exec(
                worker_python(), "-m", "agent_sessions.voice_worker", "--socket", str(sock),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=log_f, env=_worker_env(tokens, shim=_shim(d)), cwd=str(d),
                start_new_session=True,
            )  # fmt: skip
    except BaseException as e:  # no interpreter, no fork, or cancelled: the dir must go
        shutil.rmtree(d, ignore_errors=True)
        if not isinstance(e, OSError):
            raise
        raise CallError(
            503, f"the voice worker could not start: {e.strerror or e}", "worker"
        ) from None
    deadline = time.monotonic() + WORKER_START_S
    try:
        while not sock.exists():
            if proc.returncode is not None or time.monotonic() > deadline:
                tail = ["no output"]
                with contextlib.suppress(OSError):
                    lines = log_path.read_text("utf-8", "replace").strip().splitlines()
                    tail = lines[-1:] or tail
                raise CallError(503, f"the voice worker did not start: {tail[0][:200]}", "worker")
            await asyncio.sleep(0.05)
    except BaseException:
        # Failed, timed out, or the caller was cancelled while waiting: the spawned worker (and
        # its dir) never outlive this function unless it returns them.
        await asyncio.shield(Worker(proc, sock, d).stop())
        raise
    return Worker(proc, sock, d)


def worker_config(settings: dict, voice: str) -> dict:
    """What the worker is told at start: endpoints WITHOUT tokens (those are in its env)."""
    tt = settings["turn_taking"]
    return {
        "t": "config",
        "stt": {"base_url": settings["stt"]["base_url"], "model": settings["stt"]["model"]},
        "tts": {
            "base_url": settings["tts"]["base_url"],
            "model": settings["tts"]["model"],
            "voice": voice,
        },  # fmt: skip
        "in_rate": IN_RATE,
        "out_rate": OUT_RATE,
        # Silence after speech before the turn may end (VAD stop), per the turn-taking setting.
        "stop_secs": {"quick": 0.2, "natural": 0.35, "patient": 0.6}[tt],
        "interrupt": bool(settings["interrupt"]),
    }


def voice_for(settings: dict, contact: str) -> str:
    group = "woman"
    with contextlib.suppress(KeyError):
        group = hq_contacts.get(contact).voice_group or "woman"
    return settings["voices"].get(group) or settings["voices"]["woman"]


def json_msg(msg: dict) -> str:
    out = json.dumps(msg, separators=(",", ":"))
    return out


__all__ = [
    "Call",
    "CallError",
    "REGISTRY",
    "card_sentences",
    "hq_calls_settings",
    "pick_option",
    "speakable",
]
