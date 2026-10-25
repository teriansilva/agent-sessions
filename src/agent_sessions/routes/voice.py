"""`/api/voice/*` and `/ws/voice/{id}` — calling HQ (#1397 P1).

* ``GET  /api/voice/settings`` — Settings → HQ calls, tokens masked (`api_key_set`).
* ``POST /api/voice/settings`` — save a partial block (tokens origin-bound, see
  ``hq_calls_settings``).
* ``POST /api/voice/test`` — the P0 compatibility check against a DRAFT pair of endpoints, never
  persisted; a stored token is used only for the origin it was saved for.
* ``POST /api/voice/preview`` — one fixed line in a voice, returned as WAV for the browser to play.
* ``GET  /api/voice/capabilities`` — can a call start now, and if not, why; destinations.
* ``GET  /api/voice/local`` — the speech set up on this machine (#1397 P4): installed, running.
* ``POST /api/voice/local/use`` — point both endpoints at it, after the compatibility check; its
  token is read from its 0600 file server-side and never returned.
* ``POST /api/voice/calls`` — admit a call (one per instance) into an HQ conversation.
* ``DELETE /api/voice/calls/{id}`` — end it.
* ``WS /ws/voice/{id}`` — the call itself: binary PCM16 both ways, JSON control.

Every HTTP route is ``logged_in``; every state change is ``csrf_guard`` (CSRF + Origin). The socket
checks the session cookie and Origin before anything else, refuses while a password change is
forced, and then requires a first ``hello`` message carrying the CSRF token before it starts a
worker or accepts audio. The session is re-checked every ``RECHECK_S`` while the call runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import time

import websockets
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .. import (
    asgi_call,
    ask_store,
    hq_calls_settings,
    hq_contacts,
    prefs,
    speech_conformance,
    speech_endpoints,
    voice_calls,
    voice_setup,
)
from ..auth import AuthConfig, current_csrf, origin_matches, session_uid

RECHECK_S = 30.0
#: Why a call is not offered over Home Free (the P0 fallback, `docs/voice/adr-p0.md` §4).
HOME_FREE_REASON = (
    "calls over Home Free are not available yet — the audio path has not been measured on a "
    "remote connection; call from your local network"
)


def _over_home_free(conn) -> bool:
    return conn.headers.get("x-battlelab-home-free") == "1"


HELLO_TIMEOUT_S = 10.0
PREVIEW_LINES = {
    "woman": "Commander, this is HQ. All units report ready.",
    "man": "Commander, this is HQ. All units report ready.",
}


async def _body(request: Request) -> dict | JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"detail": "body must be a JSON object"}, status_code=422)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "body must be a JSON object"}, status_code=422)
    return body


def _destinations(block: dict) -> dict:
    ai = prefs.get_ai_review()
    return {
        "stt": speech_endpoints.classify_host(block["stt"]["base_url"])
        if block["stt"]["base_url"]
        else None,
        "tts": speech_endpoints.classify_host(block["tts"]["base_url"])
        if block["tts"]["base_url"]
        else None,
        "ai": speech_endpoints.classify_host(ai["base_url"]) if ai["base_url"] else None,
        "stt_host": _host(block["stt"]["base_url"]),
        "tts_host": _host(block["tts"]["base_url"]),
        "ai_host": _host(ai["base_url"]),
    }


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).netloc if url else ""
    except ValueError:
        return ""


def _draft_side(stored: dict, raw: object) -> speech_endpoints.Endpoint | str:
    """A test endpoint from a draft side `{base_url, model, api_key?}`; the stored token only for
    its own origin (`hq_calls_settings.resolve_test_token`). A string is the refusal."""
    if not isinstance(raw, dict):
        return "each side must be an object"
    base = raw.get("base_url")
    if not isinstance(base, str) or not base.strip() or not prefs.is_valid_ai_base_url(base):
        return "each side needs an http(s) base_url"
    model = raw.get("model") if isinstance(raw.get("model"), str) else ""
    if "api_key" in raw and raw["api_key"] is None:
        # The operator chose Remove token: the draft is probed WITHOUT one — never with the stored
        # token it is about to drop (else a probe passes that the saved endpoint will fail).
        return speech_endpoints.Endpoint(base.strip(), model.strip(), "")
    token, why = hq_calls_settings.resolve_test_token(stored, base, raw.get("api_key"))
    if why:
        return why
    return speech_endpoints.Endpoint(base.strip(), model.strip(), token)


_BACKGROUND: set[asyncio.Task] = set()


def _keep(task: asyncio.Task) -> None:
    """Hold a reference to a fire-and-forget task until it finishes."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _abandon(call, write: asyncio.Future, cid: str) -> None:
    """A call whose conversation could not be created: end it (freeing the one slot), then
    remove the conversation if its write landed after all."""
    await call.end("setup_failed")
    with contextlib.suppress(BaseException):
        await write  # let the write settle, whatever its outcome
    # By id, whatever the writer reported: it can raise AFTER creating the file (a failed
    # directory sync), and a header-only conversation must not stay behind (#1626 review).
    with contextlib.suppress(Exception):
        if await asyncio.to_thread(ask_store.exists, cid):
            await asyncio.to_thread(ask_store.delete_conversation, cid)


def register(
    app: FastAPI,
    *,
    cfg: AuthConfig,
    logged_in,
    csrf_guard,
    must_change: dict,
    registry=None,
) -> None:
    @app.get("/api/voice/settings")
    async def get_settings(_: str = Depends(logged_in)) -> JSONResponse:
        pub = await asyncio.to_thread(hq_calls_settings.public)
        return JSONResponse(pub)

    @app.post("/api/voice/settings")
    async def save_settings(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            await asyncio.to_thread(hq_calls_settings.save, body)
        except hq_calls_settings.SettingsError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        return JSONResponse(await asyncio.to_thread(hq_calls_settings.public))

    @app.post("/api/voice/test")
    async def test_endpoints(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        stored = await asyncio.to_thread(hq_calls_settings.get)
        stt = _draft_side(stored["stt"], body.get("stt"))
        tts = _draft_side(stored["tts"], body.get("tts"))
        for side in (stt, tts):
            if isinstance(side, str):
                return JSONResponse({"detail": side}, status_code=422)
        voice = body.get("voice")
        if not isinstance(voice, str) or not voice.strip() or len(voice) > 120:
            return JSONResponse({"detail": "voice (a short string) is required"}, status_code=422)
        report = await speech_conformance.check(stt, tts, voice.strip())
        # What THIS server saw of these endpoints is all a later save may claim (#1635 review).
        ok = {c.id: c.ok for c in report.checks}
        hq_calls_settings.observe(
            "stt", stt.base_url, stt.model, stt.api_key,
            stt_detects_language=speech_conformance.detects_language(report),  # None: unproven
        )  # fmt: skip
        hq_calls_settings.observe(
            "tts", tts.base_url, tts.model, tts.api_key,
            tts_accepts_language=ok.get("tts.language") is True,
        )  # fmt: skip
        return JSONResponse(report.as_dict())

    @app.post("/api/voice/voices")
    async def list_voices(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """The draft TTS endpoint's model and voice lists (null = enter by hand)."""
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        stored = await asyncio.to_thread(hq_calls_settings.get)
        ep = _draft_side(stored[body.get("side") if body.get("side") in ("stt", "tts") else "tts"],
                         body.get("endpoint"))  # fmt: skip
        if isinstance(ep, str):
            return JSONResponse({"detail": ep}, status_code=422)
        out: dict = {"models": None, "voices": None, "styles": None, "languages": None}
        with contextlib.suppress(speech_endpoints.SpeechError):
            out["models"] = await speech_endpoints.list_models(ep)
        if body.get("side", "tts") == "tts":
            with contextlib.suppress(speech_endpoints.SpeechError):
                out.update(await speech_endpoints.discover(ep))
            hq_calls_settings.observe(
                "tts", ep.base_url, ep.model, ep.api_key, tts_voices=out.get("voices") or [],
                tts_styles=out.get("styles") or [], tts_languages=out.get("languages") or [],
            )  # fmt: skip
        return JSONResponse(out)

    @app.post("/api/voice/preview")
    async def preview(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> Response:
        """One FIXED line in the stored TTS endpoint's voice for a group: the operator hears the
        voice before a call. The text is never taken from the request."""
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        group = body.get("group")
        voice = body.get("voice")
        contact = body.get("contact")
        block = await asyncio.to_thread(hq_calls_settings.get)
        extra: dict = {}
        speed = None
        line = None
        language = None
        if contact is not None:
            # A contact's own profile (#1397 P3), in a FIXED line naming them.
            if contact not in hq_contacts.IDS:
                return JSONResponse({"detail": "unknown contact"}, status_code=422)
            prof = hq_calls_settings.profile(block, contact)
            group = hq_contacts.get(contact).voice_group or "woman"
            voice = voice if isinstance(voice, str) and voice.strip() else prof["voice"]
            if prof.get("style"):
                extra["style"] = prof["style"]
            speed = prof.get("speed")
            lang = body.get("language")
            if lang is None:
                lang = "en"
            if not (isinstance(lang, str) and hq_calls_settings.LANG_RE.match(lang)):
                return JSONResponse({"detail": "language must be a two-letter code"}, 422)
            if lang not in hq_calls_settings.PREVIEW_CONTACT_LINES:
                return JSONResponse({"detail": f"there is no preview sentence for “{lang}” yet",
                                     "code": "no_preview_line"}, status_code=422)  # fmt: skip
            name = hq_contacts.get(contact).name
            line = hq_calls_settings.PREVIEW_CONTACT_LINES[lang].format(name=name)
            # The call's own synthesis arguments: a language only where the server proved it
            # takes the field (as a call sends it), the profile's style and speed.
            language = lang if block.get("tts_accepts_language") is True else None
        if group not in hq_contacts.VOICE_GROUPS:
            return JSONResponse({"detail": "group must be woman or man"}, status_code=422)
        if not hq_calls_settings.side_configured(block["tts"]):
            return JSONResponse({"detail": "save a text-to-speech endpoint first"}, 409)
        if not isinstance(voice, str) or not voice.strip():
            voice = block["voices"][group]
        if not voice or len(voice) > 120:
            return JSONResponse({"detail": "choose a voice first"}, status_code=422)
        side = block["tts"]
        ep = speech_endpoints.Endpoint(side["base_url"], side["model"], side["api_key"])
        pcm = bytearray()
        try:
            async for chunk in speech_endpoints.synthesize(
                ep,
                line or PREVIEW_LINES[group],
                voice=voice.strip(),
                out_rate=voice_calls.OUT_RATE,
                speed=speed,
                language=language,
                extra=extra or None,
                # A server refusing speed/style still previews the voice, as a call would speak.
                plain_fallback=lambda: None,
            ):
                pcm += chunk
        except speech_endpoints.SpeechError as e:
            return JSONResponse({"detail": e.detail, "code": e.code}, status_code=502)
        wav = speech_endpoints.pcm16_to_wav(bytes(pcm), voice_calls.OUT_RATE)
        return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def _local_running(token: str | None) -> bool:
        if not token:
            return False
        try:
            await speech_endpoints.list_models(
                speech_endpoints.Endpoint(voice_setup.LOCAL_URL, "", token)
            )
        except speech_endpoints.SpeechError:
            return False
        return True

    @app.get("/api/voice/local")
    async def local_status(_: str = Depends(logged_in)) -> JSONResponse:
        st = await asyncio.to_thread(voice_setup.status)
        token = await asyncio.to_thread(voice_setup.read_token)
        block = await asyncio.to_thread(hq_calls_settings.get)
        st["running"] = await _local_running(token)
        st["in_use"] = all(block[s]["base_url"] == voice_setup.LOCAL_URL for s in ("stt", "tts"))
        st["voices"] = voice_setup.VOICES
        return JSONResponse(st)

    @app.post("/api/voice/local/use")
    async def local_use(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """Save both endpoints as this machine's speech server — only once it passes the same
        compatibility check as **Test both**, whose optional results set the capability flags."""
        token = await asyncio.to_thread(voice_setup.read_token)
        if not token:
            return JSONResponse({"detail": "speech isn't set up on this machine", "code":
                                 "not_set_up"}, status_code=409)  # fmt: skip
        if not await _local_running(token):
            return JSONResponse({"detail": "the local speech server isn't running", "code":
                                 "not_running"}, status_code=409)  # fmt: skip
        stt = speech_endpoints.Endpoint(voice_setup.LOCAL_URL, "faster-whisper", token)
        tts = speech_endpoints.Endpoint(voice_setup.LOCAL_URL, "kokoro-v1.0", token)
        report = await speech_conformance.check(stt, tts, voice_setup.VOICES["woman"])
        if not report.usable:
            return JSONResponse({"detail": "the local speech server failed the check",
                                 "report": report.as_dict()}, status_code=502)  # fmt: skip
        ok = {c.id: c.ok for c in report.checks}
        found = await speech_endpoints.discover(tts)
        # Observed by this server, of exactly these endpoints: what the save below may claim.
        hq_calls_settings.observe(
            "stt", stt.base_url, stt.model, token,
            stt_detects_language=speech_conformance.detects_language(report),  # None: unproven
        )  # fmt: skip
        hq_calls_settings.observe(
            "tts", tts.base_url, tts.model, token,
            tts_accepts_language=ok.get("tts.language") is True,
            tts_voices=found["voices"] or [], tts_languages=found["languages"] or [],
        )  # fmt: skip
        patch = {
            "stt": {"base_url": voice_setup.LOCAL_URL, "model": "faster-whisper", "api_key": token},
            "tts": {"base_url": voice_setup.LOCAL_URL, "model": "kokoro-v1.0", "api_key": token},
            "voices": dict(voice_setup.VOICES),
            "tts_voices": found["voices"] or [],
            "tts_languages": found["languages"] or [],
            "tts_styles": [],
            "stt_detects_language": speech_conformance.detects_language(report),
            "tts_accepts_language": bool(ok.get("tts.language")),
        }
        try:
            saved = await asyncio.to_thread(voice_setup.save_if_token_current, token, patch)
        except hq_calls_settings.SettingsError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        if not saved:  # the token was rotated while this checked: never save the old one
            why = "the speech server's token changed meanwhile — press Use this machine again"
            return JSONResponse({"detail": why, "code": "token_changed"}, status_code=409)
        pub = await asyncio.to_thread(hq_calls_settings.public)
        return JSONResponse({"settings": pub, "report": report.as_dict()})

    @app.get("/api/voice/capabilities")
    async def capabilities(request: Request, _: str = Depends(logged_in)) -> JSONResponse:
        block = await asyncio.to_thread(hq_calls_settings.get)
        configured = hq_calls_settings.configured(block)
        ready, why = (False, "")
        if configured:
            ready, why = await voice_calls.runtime_ready()
        ai = prefs.public_ai_review()
        reason = ""
        if not configured:
            reason = "set up calls first: speech endpoints and voices"
        elif not ready:
            reason = why
        elif not ai.get("configured"):
            reason = "HQ's AI endpoint is not configured"
        elif _over_home_free(request):
            reason = HOME_FREE_REASON
        current = voice_calls.REGISTRY.current
        if os.environ.get("AGENT_SESSIONS_VOICE_PYTHON"):
            runtime_note = "the call runtime is overridden (AGENT_SESSIONS_VOICE_PYTHON)"
        else:
            runtime_note = await asyncio.to_thread(voice_setup.stale) or ""
        if not ready and configured and voice_setup.runtime_python() is None:
            reason = "the call runtime isn't installed: run `agent-sessions voice setup`"
        return JSONResponse(
            {
                "configured": configured,
                "runtime": ready,
                "available": configured
                and ready
                and bool(ai.get("configured"))
                and not _over_home_free(request),
                "reason": reason,
                "runtime_note": runtime_note,
                "destinations": _destinations(block),
                "captions": block["captions"],
                "mic": block["mic"],
                "active_call": current.public()
                if current is not None and current.state != "ended"
                else None,
            }
        )

    @app.post("/api/voice/calls")
    async def admit_call(
        request: Request, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        if _over_home_free(request):
            return JSONResponse({"detail": HOME_FREE_REASON, "code": "home_free"}, status_code=409)
        block = await asyncio.to_thread(hq_calls_settings.get)
        if not hq_calls_settings.configured(block):
            return JSONResponse({"detail": "calls are not set up", "code": "unconfigured"}, 409)
        if not prefs.public_ai_review().get("configured"):
            return JSONResponse({"detail": "HQ's AI endpoint is not configured", "code": "ai"}, 409)
        ready, why = await voice_calls.runtime_ready()
        if not ready:
            return JSONResponse({"detail": why, "code": "runtime"}, status_code=409)
        contact = hq_contacts.resolve(body.get("contact"))
        cid = body.get("conversation_id")
        if cid is not None:
            if not ask_store.valid_cid(cid) or not await asyncio.to_thread(ask_store.exists, cid):
                return JSONResponse({"detail": "conversation not found"}, status_code=404)
        profile = voice_calls.profile_for(block, contact)
        # Reserve the one call slot FIRST: a refused (busy) call must not leave an empty
        # conversation behind (each one counts against the store's retention).
        new_cid = ask_store.new_cid() if cid is None else None
        try:
            call = await voice_calls.REGISTRY.admit(
                cid=cid or new_cid,
                answers=block.get("answers", "brief"),
                contact=contact,
                voice=profile["voice"],
                profile=profile,
                operator=asgi_call.Operator.from_scope(request.scope),
                app=request.app,
                registry=registry,
                session_csrf=current_csrf(cfg, request) or "",
            )
        except voice_calls.CallError as e:
            return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status)
        if new_cid is not None:
            write = asyncio.ensure_future(asyncio.to_thread(ask_store.create, new_cid))
            try:
                await asyncio.shield(write)
            except Exception as e:
                # The write failed (and has settled): never a stranded "dialing" call, never a
                # leftover conversation — cleaned up before answering.
                await _abandon(call, write, new_cid)
                if isinstance(e, ask_store.StoreRefused):
                    return JSONResponse({"detail": str(e)}, status_code=503)
                return JSONResponse({"detail": "the call could not be set up"}, 500)
            except BaseException:
                # This request was cancelled while the write runs on: finish in the background
                # once it settles (it may still land).
                _keep(asyncio.ensure_future(_abandon(call, write, new_cid)))
                raise
        if block.get("language", "auto") != "auto":
            call.fixed_language = call.language = block["language"]
        call.settings = block
        call.tts_takes_language = bool(block.get("tts_accepts_language"))
        det = block.get("stt_detects_language")
        # Tri-state: True / False once tested, None (unknown: spoken confirmation fails closed).
        call.detects_language = det if isinstance(det, bool) else None
        call.dial_timer = asyncio.create_task(voice_calls.end_if_never_joined(call))
        return JSONResponse(call.public(), status_code=201)

    @app.delete("/api/voice/calls/{call_id}")
    async def end_call(
        call_id: str, _u: str = Depends(logged_in), _c: None = Depends(csrf_guard)
    ) -> JSONResponse:
        call = voice_calls.REGISTRY.get(call_id)
        if call is None:
            return JSONResponse({"detail": "no such call"}, status_code=404)
        await call.end("hung_up")
        return JSONResponse(call.public())

    async def _end_call_on_logout() -> None:
        await voice_calls.REGISTRY.end_all("signed_out")

    app.state.logout_hooks = [*getattr(app.state, "logout_hooks", []), _end_call_on_logout]

    @app.websocket("/ws/voice/{call_id}")
    async def call_socket(ws: WebSocket, call_id: str) -> None:
        await ws.accept()

        async def reject(code: int, reason: str = "") -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=code, reason=reason[:120])

        if session_uid(cfg, ws) is None:
            return await reject(4401)
        if not origin_matches(cfg, ws) or must_change["v"]:
            return await reject(4403)
        if _over_home_free(ws):
            return await reject(4409, "not over Home Free")
        call = voice_calls.REGISTRY.get(call_id)
        if call is None or call.state != "dialing" or call._to_client is not None:
            return await reject(4404, "no such call")
        # First-message CSRF proof, before any worker starts or any audio is accepted.
        try:
            first = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S)
            hello = json.loads(first) if len(first) <= voice_calls.MAX_CONTROL_FRAME else {}
        except (TimeoutError, ValueError, WebSocketDisconnect, RuntimeError, KeyError):
            return await reject(4400, "hello expected")
        # The hello carries this session's CSRF token, and it must be the token of the session
        # that ADMITTED the call: another signed-in session (another device) cannot take over a
        # call's audio. A refused socket leaves the call to its own session (it is not ended
        # here); a call no socket ever joins ends after DIAL_TIMEOUT_S. Hanging up stays open to
        # any of the operator's sessions (DELETE).
        want = current_csrf(cfg, ws) or ""
        got = hello.get("csrf") if isinstance(hello, dict) else None
        if (
            not isinstance(hello, dict)
            or hello.get("t") != "hello"
            or not isinstance(got, str)
            or not want
            or not hmac.compare_digest(got, want)
            or not hmac.compare_digest(want, call.session_csrf)
        ):
            return await reject(4403, "bad csrf")

        # Re-check AFTER the hello wait, with no await before the claim below: a second socket
        # (or a call ended meanwhile) must not start a second worker or steal this one's audio.
        if call.state != "dialing" or call._to_client is not None:
            return await reject(4409, "call already joined or ended")

        send_lock = asyncio.Lock()

        async def to_client(msg: dict | bytes) -> None:
            async with send_lock:
                if isinstance(msg, bytes):
                    await ws.send_bytes(msg)
                else:
                    await ws.send_text(voice_calls.json_msg(msg))

        worker = wsock = None
        tasks: list[asyncio.Task] = []

        async def to_worker(msg: dict | bytes) -> None:
            await wsock.send(msg if isinstance(msg, bytes) else voice_calls.json_msg(msg))

        async def from_worker() -> None:
            async for msg in wsock:
                if isinstance(msg, bytes):
                    if len(msg) <= voice_calls.MAX_AUDIO_FRAME * 4:
                        await call.client(msg)
                    continue
                if len(msg) > voice_calls.MAX_CONTROL_FRAME * 4:
                    continue
                with contextlib.suppress(ValueError):
                    ev = json.loads(msg)
                    if isinstance(ev, dict):
                        await call.on_worker_event(ev)
            if call.state != "ended":
                await call.end("worker_failed", detail="the voice worker stopped")

        async def from_client() -> None:
            window_start, window_bytes = time.monotonic(), 0
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                if data is not None:
                    now = time.monotonic()
                    if now - window_start >= 1.0:
                        window_start, window_bytes = now, 0
                    window_bytes += len(data)
                    if (
                        len(data) > voice_calls.MAX_AUDIO_FRAME
                        or window_bytes > voice_calls.MAX_INGRESS_BPS
                    ):
                        continue  # over budget: dropped, never queued
                    if not call.muted and call.state != "ended":
                        await call.worker(data)
                    continue
                text = msg.get("text")
                if text is None or len(text) > voice_calls.MAX_CONTROL_FRAME:
                    continue
                with contextlib.suppress(ValueError):
                    ev = json.loads(text)
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("t") == "end":
                        await call.end("hung_up")
                        return
                    if ev.get("t") == "needs_you":
                        await call.on_needs_you_request()
                    if ev.get("t") == "card_decided":
                        await call.on_card_decided(ev.get("id"))
                    if ev.get("t") == "mute":
                        call.muted = bool(ev.get("on"))
                        if call.muted:
                            await call.void_challenge()
                        await call.worker({"t": "mute", "on": call.muted})
                        await call.client({"t": "muted", "on": call.muted})
                    if ev.get("t") == "played":
                        # The browser has PLAYED a say out: a read-back may now arm (#1397 P2);
                        # HQ was heard (the silence clock, #1668); a farewell may end the call.
                        await call.on_played(ev.get("id"))

        async def watchdog() -> None:
            while call.state != "ended":
                await asyncio.sleep(min(RECHECK_S, 5.0))
                if session_uid(cfg, ws) is None or must_change["v"]:
                    await call.end("signed_out")
                else:
                    # No idle or duration end (#1668): HQ's farewell, or no answer to its
                    # still-there question, are the only timed ends.
                    await call.check_silence()

        async def teardown() -> None:
            """Everything this socket started, stopped — on any exit, cancellation included."""
            if call.state != "ended":
                await call.end("disconnected")
            for t in tasks:
                t.cancel()
            if call.turn_task is not None:
                call.turn_task.cancel()  # stops speaking it; the HQ turn itself is shielded
            if wsock is not None:
                with contextlib.suppress(Exception):
                    await wsock.close()
            if worker is not None:
                await worker.stop()
            call.mark_torn_down()
            call._to_client = None
            call._to_worker = None
            with contextlib.suppress(Exception):
                await ws.close(code=1000)

        # The claim, and everything after it, inside ONE cancellation-safe scope: whatever
        # raises or is cancelled from here on, teardown runs and the call slot is freed.
        call._to_client = to_client  # no await since the re-check above
        # With the claim, not later: from here a hang-up must wait for THIS socket's teardown
        # before the slot frees (an end() in the awaits below would otherwise free it early).
        call.worker_started = True
        try:
            await call.client({"t": "state", "state": "dialing"})
            # The settings the call was admitted with (its language gate came from them too).
            block = call.settings or await asyncio.to_thread(hq_calls_settings.get)
            if call.state == "ended":
                return  # hung up meanwhile: no worker for an ended call (teardown frees the slot)
            try:
                worker = await voice_calls.start_worker(block)
                wsock = await websockets.unix_connect(
                    str(worker.sock), "ws://worker/call", max_size=voice_calls.MAX_AUDIO_FRAME * 2
                )
                call._to_worker = to_worker
                await to_worker(voice_calls.worker_config(block, call.voice, call.profile))
            except Exception as e:  # noqa: BLE001 — ANY failure here ends the call
                detail = e.detail if isinstance(e, voice_calls.CallError) else type(e).__name__
                await call.end("worker_failed", detail=str(detail)[:160])
                with contextlib.suppress(Exception):
                    await ws.close(code=4500, reason="worker failed")
                return
            tasks = [
                asyncio.create_task(from_worker()),
                asyncio.create_task(from_client()),
                asyncio.create_task(watchdog()),
                asyncio.create_task(call.wait_ended()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Shielded: a cancelled handler still stops its worker and frees the call slot.
            await asyncio.shield(teardown())
