# Dictation

Dictation is push-to-talk into the [compose bar](/guide/terminal): press and hold, speak, release.
It exists because typing a paragraph of instruction on a phone keyboard is the slowest part of
using BattleLab from a phone.

It uses the **browser's own** speech recognition — there is no audio upload, no transcription
service, and no configuration.

## What it does to your draft

Spoken text is **appended to the draft that existed when you started**, with a single separator
inserted only where one is needed, so dictation reads as a continuation of what you had already
typed rather than replacing it.

That behaviour is more carefully guarded than it looks. A browser `SpeechRecognition` session is
not a recording you control: the engine decides when it has ended, and in non-continuous mode it
ends after **every single utterance** — so a naive implementation stops the whole dictation the
moment you pause for breath. Press-and-hold is held open across those engine-initiated endings.

Anything that edits the draft destructively is gated on a positive, in-stream signal that the
engine actually produced the state being acted on — never on a timer and never on a guess about
which browser you are using.

## When it does not work

Failures are reported specifically rather than as one generic "microphone blocked", because the
distinction is the whole diagnosis:

- **mic blocked** — the browser denied permission for this site.
- **no microphone found** — no capture device.
- Other `SpeechRecognition` error codes are surfaced with their own note.

::: warning It needs a secure context and an explicit policy
Browser speech recognition requires HTTPS, so dictation will not work over plain HTTP. BattleLab
also sets its own `Permissions-Policy` header, and it must permit the microphone for the app's own
origin (`microphone=(self)`) — a blanket `microphone=()` disables dictation entirely while
producing exactly the generic "microphone blocked" message that makes the cause hard to find.
Those headers come from the **app**, not from your reverse proxy.
:::

::: info Verified against
Commit `218cf3a` — `web/src/components/terminal/Compose.tsx`; `web/src/lib/dictation.ts`; `src/agent_sessions/security_headers.py § Permissions-Policy`.
:::
