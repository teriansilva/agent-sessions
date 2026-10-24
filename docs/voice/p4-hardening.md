# Calling HQ — P4: self-hosted setup and hardening (#1625)

This builds on [the P0 ADR](adr-p0.md). It records the security review of the whole voice
surface, the browser/OS matrix, the rollback drill, and the local speech server's numbers.

## 1. Security review — the whole voice surface

| Surface | What protects it | Pinned by |
|---|---|---|
| **HTTP routes** (`/api/voice/*`) | All are `logged_in`; every state change is `csrf_guard` (CSRF + Origin). `local/use` reads the local token server-side, and no response carries a token (`api_key_set` only). | `test_voice_calls.py`, `test_voice_setup.py` |
| **The call socket** (`/ws/voice/{id}`) | Cookie, Origin and the forced-password gate are checked before anything else. A first `hello` must carry the CSRF token of the session that **admitted** the call before a worker starts or audio is accepted. The session is re-checked every 30 s; control frames ≤ 8 KiB; audio over budget is dropped. | `test_voice_calls.py` |
| **Over Home Free** | Refused, with the reason (the proxy stamps `X-BattleLab-Home-Free`). | `test_voice_calls.py` |
| **Outbound speech requests** | Only `speech_endpoints._request` (inventoried):<br>• the host is resolved once and the connection pinned to that vetted IP, with Host and SNI kept;<br>• link-local, multicast, unspecified and reserved addresses refused;<br>• no redirects, no ambient proxy, TLS verified, every reply bounded (the resampled tail included);<br>• a token only goes to the origin it was saved for. | `test_speech_endpoints.py`, `test_prompts_registry.py` |
| **Model downloads** | `voice_setup._fetch` (inventoried), fixed URLs, TLS verified. Each file is size- and sha256-checked before it's used; a mismatch deletes the `.part` file. | `test_voice_setup.py` |
| **Python packages** | Hash-locked (`--require-hashes --no-deps`) and **wheels only** (`uv --no-build` / `pip --only-binary :all:`): building a source archive would run an unhashed, unpinned build backend, so a package without a wheel fails setup instead. `docopt` (num2words' CLI only, source-only) is deliberately left out. No resolver runs on the operator's machine. | `test_voice_setup.py` (hashes; wheels-only argv; no source-only package) |
| **The call worker** | One process per call:<br>• a private `0700` dir, sanitized environment (no app secrets), its speech tokens in the env (never argv);<br>• a Unix socket only;<br>• no app credentials, no route access.<br>Its package shim points at the running release's code. | `test_voice_calls.py`, `test_voice_setup.py` |
| **The local speech server** | • **No TCP listener at all.** It serves only a Unix socket, `voice/speech.sock` (bound `0600`, in the `0700` voice dir). The settings name it by `http://127.0.0.1:8766/v1`, and `speech_endpoints` maps that origin to the socket: nothing is ever sent to the TCP port, so another local user who binds it gets no token and no audio.<br>• Bearer token compared in constant time; token in a `0600` env file (`EnvironmentFile=`), never in argv, the unit or a log; no access log.<br>• Uploads ≤ 10 MB, text ≤ 4,000 chars, one request at a time per model.<br>• systemd: `MemoryMax=3G`, `CPUQuota=200%`, `Nice=10`, `NoNewPrivileges=yes`, `PrivateTmp=yes`. | `test_voice_setup.py` |
| **Acting by voice** | A card is read back as fixed words; the challenge arms only on the browser's `played` ack. A confirmation counts only if it started after arming (echo guard); single use, 20 s.<br>The classifier is strict (confirm verb, no negation, an ordinal for several cards, "everything" refused) and English-only, failing closed on an unknown or other heard language, fixed English included.<br>Settlement uses the card's own route as the operator, so every guard a tap meets applies. | `test_voice_confirm.py`, `test_voice_calls.py` (through the socket) |
| **What is spoken** | HQ's stored (redacted) answer, made speakable, plus fixed sentences. Raw tool output never reaches TTS; the conformance report never carries a transcript. | `test_voice_calls.py`, `test_speech_endpoints.py` |
| **Data** | No audio is stored. A heard utterance becomes an HQ turn (`via: "call"`) and is stored like a typed one. | `test_voice_calls.py` |

**Residual risks, accepted and stated:**

- **Kokoro's checksums are trust-on-first-use.** The upstream release publishes no digests, so the
  pins were taken from the files downloaded on 2026-10-23. Hugging Face's faster-whisper files are
  pinned by revision and by its LFS sha256.
- **Other users on the same host.** Loopback is no boundary between local users, so the server has
  no TCP port: it listens only on a socket in the owner-only voice dir, and clients reach it there
  alone. The token is only readable by the user that owns BattleLab (0600 file; the server's
  environment in `/proc` is owner-only).
- **Spoken words are user input like typed words.** A spoken prompt injection has the same reach as
  a typed one: HQ's guard and the card routes apply unchanged.
- **Echo handling relies on the browser's echo cancellation** (ADR A1). The echo guard and the
  played-ack keep HQ's own read-back from ever confirming it.

## 2. Browser / OS matrix

**Verified in CI:** Playwright with a fake microphone (`--use-fake-device-for-media-stream`) on the
`desktop` (Desktop Chrome) and `mobile` (Pixel 7) projects. It covers:

- dialing, connected and ended states;
- captions;
- the played ack and the challenge;
- Settings, including all 13 Generals on mobile;
- focus order, 44 px targets, and Copy at 320 px.

**Needs the operator's devices** (a checklist for the live acceptance on #1397):

- [ ] Chrome desktop (macOS / Windows / Linux), laptop speakers: barge-in without self-interruption.
- [ ] Chrome Android: call, screen off for 30 s, back.
- [ ] Safari iOS (and the installed PWA): microphone permission, playback after a tap, background behaviour.
- [ ] Firefox desktop: capture worklet and playback.
- [ ] A Bluetooth headset (switching profiles mid-call).
- [ ] The installed PWA in the background: the call ends cleanly or keeps going, never a stuck microphone.

## 3. Drills (throwaway `AGENT_SESSIONS_HOME`, 2026-10-23)

These ran against a throwaway home with `AGENT_SESSIONS_NO_SERVICE=1` (no user unit; the server ran
under `voice serve`), from a read-only snapshot of the branch. The live install was never touched
(workflow §0).

| Drill | Result |
|---|---|
| **Fresh setup** | Done. One pointer `voice/current → sets/<rt>-<srv>`, the tree at `0700`, `speech.env` at `0600`, the stamp written, no unit (none expected). With uv's wheel cache it builds in minutes; on today's ~150 KB/s PyPI link it was mostly download time. |
| **A failure mid-setup** | A source file vanished during a build (an operator error in the first drill). No pointer, stamp or token was written, so nothing became active. It surfaced as a traceback; now it is a named failure (`FileNotFoundError: …`, tested). |
| **Re-run with nothing new** | 1.1 s: verified generations and models are reused and nothing is re-downloaded. |
| **Upgrade** (a release with a changed runtime lock) | `status` reported `stale` before. Setup built the new runtime generation beside the old one in 7.2 s (uv cache), switched both kinds in one rename, and recorded the previous set. |
| **Rollback** | `--rollback` pointed `current` back at the previous set; `status` reports the newer release's lock as `stale` again. |
| **Token rotation** | It produced a new token, with `speech.env` still `0600`. **Found:** with no user unit, a running `voice serve` kept the old token while the settings would take the new one. Rotation now refuses while an unmanaged server answers (tested). |
| **Remove** | With `voice serve` running: refused (found in the drill and fixed; previously it would have deleted files under the running server). After the server stopped: the runtime, server, sets, stamp and token were removed, and the models (720 MB) were kept. A reinstall took 14 s and came back non-stale. |
| **Use this machine + a live call** | See §4. |

## 4. The local speech server, measured

This is the same host as the drills: a shared VM, CPU only, with other agents running.

**Speech-to-text** (faster-whisper int8, one 2.6 s phrase, warm). faster-whisper encodes a full
30 s window, so the cost is per utterance, not per second of speech; a shorter `chunk_length` didn't
change it.

| Model | 2 threads | 4 threads | 8 threads | Transcript |
|---|---|---|---|---|
| `small` | 4.2 s | 3.7 s | 3.0 s | exact |
| `base` | **1.6 s** | 1.3 s | — | exact |

**Decision:** `base` is the CPU default (`--stt small` for faster machines). The unit's budget
(`CPUQuota=200%`) matches the 2-thread column.

**Text-to-speech** (Kokoro v1.0 int8): real-time factor 0.41 and first audio after about 1.0 s for one
sentence. Kokoro returns a sentence at a time, so the conformance check reports "no streaming"; the
worker's sentence look-ahead (ADR A5) hides it between sentences.

**Use this machine:** 6.0 s, including the full compatibility check. It passed usable, with
`stt.language` and `tts.language` proven, so both language capabilities were saved on. The
destinations read `loopback`, and no response carried the token. Re-run 2026-10-24 with the
server on its Unix socket (`0600`, no TCP listener on 8766): the compatibility check passed usable
with detection proven, and a two-sentence synthesis streamed 160,768 bytes of PCM.

**A live call through what setup installed** (isolated instance, a real HQ turn on the operator's AI
endpoint, the question spoken by the local Kokoro):

| Step | After the question ended |
|---|---|
| The worker starts (from `voice/current/runtime/bin/python`, no override) | Connected 1.5 s after dialing |
| The utterance is transcribed and shown | 2.8 s |
| "One moment, Commander." (the 1.2 s acknowledgement) | about 4.0 s |
| The answer text (HQ ran `fleet_status`) | 11.6 s; most of it is the HQ model |
| The first audio of the answer | 12.6 s; Kokoro speaks 1.0 s after the text |

The turn was stored with `via: call`. Server memory was about 600 MB resident (measured with
`small` loaded).

Whether this latency is acceptable is the operator's call, on #1397. The P0 measurements with
pm-core's GPU (utterance in under 1 s, first audio about 2 s) are the comparison.
