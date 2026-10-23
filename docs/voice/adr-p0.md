# Calling HQ — P0 decisions and measurements (#1397, #1618)

Status: **provisional.** The speech contract and the probe are accepted. The natural-call targets are not
decided here: the barge-in latency below misses the epic's ≤ 0.2 s target, and the operator decides at the
first live call (P1 acceptance) whether to accept, tune or change the gate. Measured 2026-10-23 against the operator's LAN GPU host ("pm-core",
an RTX 5090 + 3090 box). The speech servers on it are reached over the LAN, configured exactly as an
operator would configure any OpenAI-compatible speech endpoint.

| Side | Server | Model |
|---|---|---|
| STT | pm-core ASR, OpenAI-compatible `/v1/audio/transcriptions` | faster-whisper large-v3-turbo (fp16) |
| TTS | pm-core VoxCPM2, OpenAI-compatible `/v1/audio/speech` | VoxCPM2; voices `aria atlas ember nova sage`; 48 kHz WAV |

## 1. The speech contract

What BattleLab sends and accepts. It is implemented once in `src/agent_sessions/speech_endpoints.py`,
the only module that talks to a speech server.

- **Transcription:** `POST <base>/audio/transcriptions`, multipart.
  - Fields: `file` (16-bit mono WAV), `model` (default `whisper-1`), optional `language`, and `response_format=json`.
  - Reply: `{"text": …}`. A plain-text reply is also accepted.
- **Synthesis:** `POST <base>/audio/speech`, JSON `{model, input, voice, response_format: "pcm"}` (+ `speed`, `language`).
  - The reply is decided by its bytes and content type, never assumed:
    - raw 16-bit mono PCM at 24 kHz (OpenAI's `pcm`);
    - a WAV stream: the header is parsed, extra chunks are tolerated, it is downmixed to mono and resampled.
  - Compressed audio (mp3/ogg/opus/aac/flac) is refused.
  - A streamed reply is consumed as it arrives.
- **Discovery:**
  - Models: `GET <base>/models`.
  - Voices: `GET <base>/audio/voices` (Kokoro-FastAPI), else `GET <base>/health` → `voices` (VoxCPM).
  - Otherwise there is no list, and the operator types the model or voice.
- **Token:**
  - Optional.
  - Sent only as `Authorization: Bearer`, only to the endpoint's own origin: the AI key's rule, applied per side.
  - Never on a command line and never in a URL.
- **Transport rules:**
  - `trust_env=False`, no redirects followed, TLS verified.
  - Hosts resolving to link-local, multicast, unspecified or reserved addresses are refused before any request.
  - Loopback and LAN addresses are allowed.
- **Bounds:**
  - transcript replies ≤ 256 KiB;
  - synthesized audio ≤ 48 MiB per request;
  - uploads ≤ 16 MiB;
  - text ≤ 4,000 chars.

**Conformance check** (`speech_conformance.check`, run by Settings → **Test both** and by
`scripts/voice_conformance.py`).

- **Required capabilities:**
  - synthesis returns usable PCM in the chosen voice;
  - the synthesized probe phrase is transcribed back recognisably.
- **Optional capabilities**, named when missing:
  - model list ("enter the model");
  - voice list ("enter the voice");
  - streaming (first audio waits for the whole sentence);
  - first audio ≤ 1.5 s;
  - synthesis faster than real time.

Result against pm-core: **usable**. STT and TTS model lists and the voice list are present, and transcription
comes back word-perfect. Flagged as optional misses: first audio ≈ 1.8 s, and synthesis slower than real time.

## 2. Measurements (pm-core, warm, median of 3)

**TTS: VoxCPM2, synthesis only.**

| Voice | Short (3 words) first / RTF | Medium (10 words) first / RTF | Long (30 words) first / RTF |
|---|---|---|---|
| atlas | 1.00 s / 1.03 | 2.61 s / 1.17 | 6.41 s / 1.76 |
| aria | 0.74 s / 1.11 | 2.51 s / 1.32 | 5.31 s / 1.31 |
| nova | 0.74 s / 0.70 | 2.34 s / 1.38 | 5.99 s / 1.55 |
| sage | 0.76 s / 0.78 | 2.25 s / 1.20 | 5.28 s / 2.12 |
| ember | 1.08 s / 0.89 | 2.16 s / 1.23 | 6.03 s / 1.28 |

RTF = synthesis time ÷ audio length. Above 1.0, the server is slower than playback.

**Cold starts (n = 1 each, first request after the servers had been idle; not a distribution):**
- the first TTS request of the session took **25.1 s** (aria);
- the first STT request took **3.6 s**;
- the very next requests ran at the warm figures above.

A call's first sentence may therefore be slow when pm-core has been idle. The worker's own cold start
(process + Silero + Smart Turn) is 1.3–1.7 s on every call.

**STT: faster-whisper large-v3-turbo.**
- 0.24–0.30 s for ~1 s of speech, 0.46–0.57 s for ~4 s, 0.68–0.76 s for ~10 s.
- Every phrase was recognised; the one miss was "On it" → "Onnit", from a TTS voice.

**The Pipecat worker, end to end** (`scripts`-style spike: synthesized questions streamed as microphone
audio in 20 ms frames, in real time):

| Step | Result |
|---|---|
| Worker start (process + Silero + Smart Turn v3 loaded) | 1.4–1.7 s |
| Speech end → utterance delivered (Smart Turn end-of-turn + STT) | **0.86–0.99 s** |
| Reply text → first audio at the client (7-word first chunk, one sentence of look-ahead) | **1.85–2.6 s**; 5.5 s before chunking and streaming |
| Barge-in: speech onset while HQ speaks → `interrupt` | **0.37–0.71 s** |
| A pause mid-sentence ("Wait, stop. … What about the docs mission?") | one or two turns, every word delivered (§4) |

**What this means for a call.** Perceived response = ~0.9 s (turn end) + the HQ turn (model and tools,
usually seconds) + ~2 s (first audio). The fixed acknowledgement after 1.2 s ("One moment, Commander.")
covers the HQ turn, so the line is never silent for long.

**No p95 is claimed:** n = 3 per cell is enough for a median, not a tail.

**Gaps against the epic's targets** (for the operator's decision; not accepted here):
- **Barge-in** is 0.37–0.71 s, against a ≤ 0.2 s target. VAD's `start_secs` is 0.2 s by design: shorter makes coughs interrupt.
- **VoxCPM2's slower-than-real-time synthesis** can leave short pauses between sentences of a long answer.

Neither stops P1 from being **built**: the operator asked for P0–P4 to be implemented (2026-10-23; the
gate change is recorded on #1397). Whether these numbers are **acceptable** is the operator's decision at
the first live call, before calls are relied on, and this ADR does not accept the ≤ 0.2 s barge-in target as
met. Both improve with a faster TTS (Kokoro: RTF ≪ 1, first audio ≈ 0.2 s on a GPU). That is a
configuration change for the operator, not a code change, and P4's guided setup offers it.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| A1 | **One transport for P1: binary PCM + JSON over the app's WebSocket**, opened with `openAppSocket`, so direct and Home Free calls are the same code. Browser capture via `getUserMedia({echoCancellation, noiseSuppression, autoGainControl})` + an AudioWorklet; playback via an AudioWorklet queue the client flushes on `{"t":"interrupt"}`. **SmallWebRTC is deferred.** | One path to build and test. The worker stays private behind the app, with no ICE/UDP and no public listener. Chrome's and Safari's echo cancellation cover page audio. If live calls show echo, P4 revisits WebRTC on LAN. |
| A2 | **The worker is a separate process per call**, `python -m agent_sessions.voice_worker`. It serves one WebSocket on a Unix socket in a private `0700` temp dir, with `cwd` there. It is spawned with a sanitized environment (`PATH`, `HOME`, locale, Python vars, the two speech tokens), stdout to `/dev/null`, stderr to a file in its dir, and native thread pools capped at 2. | Crash isolation and least privilege. **Found in P0:** an unread stderr pipe blocked the worker's event loop once Pipecat logged enough. The worker also logs at WARNING. |
| A3 | **The worker is a speech terminal.** It hears (Silero VAD → Smart Turn v3 → segmented STT) and speaks (TTS), and knows nothing about HQ. The app runs every HQ turn in-process, with the operator from the call's socket. | No new authority. The worker never holds app credentials and cannot reach a route. |
| A4 | **A turn's text is complete when every speech segment of it has come back from STT.** Transcripts are counted where they land, at the bridge; empty or failed segments are counted by the STT service; there is a 3 s grace. | **Found in P0:** counting completion inside STT raced the transcript frame to the bridge, and an empty turn was flushed. |
| A5 | **Speech is chunked for slow TTS:** sentences, long ones split at clause boundaries (≤ 14 words), the first chunk ≤ 7 words. Audio is passed on as it streams, and one sentence is synthesized ahead. | First audio 5.5 s → ~2 s on VoxCPM2. More look-ahead slowed the first chunk through GPU contention. |
| A6 | **Interrupt on VAD start** (`VADUserTurnStartStrategy`, VAD `start_secs` 0.2, confidence 0.7); the setting "Interrupt HQ: off" disables it. Turn end: Smart Turn v3 over VAD `stop_secs` of 0.2 / 0.35 / 0.6 for Quick / Natural / Patient. | Transcription-based start (min words) would interrupt only after the phrase ended. |
| A7 | **Pipecat's RTVI client protocol is off** (`enable_rtvi=False`, and the serializer ignores RTVI frames). | Its messages leaked onto our wire in the first spike. |
| A8 | **Endpoint tokens**: the AI key's origin rule per side, evaluated in `prefs.mutate_block`'s lock; the draft test uses a stored token only for its own origin; tokens are optional. | Self-hosted servers usually have no token; OpenAI needs one. |
| A9 | **Limits:** one call per instance; 60 min maximum; 5 min idle; 64 KiB audio frames; 128 KiB/s ingress (excess dropped, never queued); 8 KiB control frames; the session re-checked every 30 s. | The epic's provisional budgets, confirmed workable in the spike. |

## 4. Not measured here, and why

- **Browser echo cancellation, real microphones, mobile background behaviour.** These need a person and a device.
  - The P1 browser tests use fake media devices.
  - The operator's first live call is the acceptance check.
- **Home Free audio.** The P1 transport rides `openAppSocket` like the terminal, so the code path is the same,
  but its budget (barge-in, queues, terminal-echo degradation) can only be measured on the operator's remote
  path. Per the epic's fallback, **P1 ships calls as unavailable over Home Free, with the reason shown**. The
  Home Free proxy marks the requests it forwards, and the voice routes refuse admission there. A follow-up
  measures the remote path live and lifts the refusal.
- **Recordings:** none were made. The spike used synthetic TTS audio as its microphone, so no human voice was captured.

## 5. Dependencies (pinned in `pyproject.toml`'s `voice` extra from P1)

| Component | Version | License | Role |
|---|---|---|---|
| pipecat-ai | 1.13.0 | BSD-2-Clause | Pipeline, VAD/turn plumbing, WebSocket transport |
| Smart Turn v3 (ships in pipecat) | bundled ONNX | BSD-2-Clause | End-of-turn |
| Silero VAD (ships in pipecat) | bundled ONNX | MIT | Voice activity |
| onnxruntime | via pipecat | MIT | Runs both models on CPU |
| faster-whisper (server side, operator's) | — | MIT | STT on pm-core |
| VoxCPM2 (server side, operator's) | — | Apache-2.0 | TTS on pm-core |

The server-side models are the operator's, behind endpoints, and are not shipped by BattleLab.
