# Calling HQ

You can **call HQ** and talk to it like a phone call. HQ answers out loud in the voice of the General you
called, you can interrupt it mid-sentence, and it can act on what you say: send a nudge, start a
session, answer a Needs-you question. Everything happens through the same [HQ](/guide/hq) you type
to, with the same tools, limits and confirmations.

The call runs **in the browser**: your microphone, BattleLab, and a speech server you choose. It is
not a phone line, and no audio is stored anywhere.

## Set it up

Calls need two speech services: speech-to-text, which hears you, and text-to-speech, which speaks
HQ's answers. You configure them in **Settings → AI → HQ calls**. You have three options.

### On this machine — free, offline after one download

Run this once on the machine BattleLab runs on:

```bash
agent-sessions voice setup
```

It says what it will download (about 700 MB) and asks first. Then it installs:

- the **call runtime**: the conversation engine that handles turn-taking and interruptions;
- a small **speech server**: faster-whisper to hear you, and Kokoro for HQ's voices;
- its **models**. Each one is checked against a pinned checksum before it is used.

The speech server runs as a user service (`agent-sessions-speech`). It opens no network port: it
answers only on a private socket file that only your user can open, behind a random access key, and
has a budget of 2 CPUs and 3 GB of memory. (Settings shows it as `http://127.0.0.1:8766/v1`, but
BattleLab sends those requests to the socket, never to that port.) In Settings, press
**Use this machine**. That fills in both speech services and the two starting voices; the access
key never leaves the server.

- `--stt small` picks a larger speech-recognition model (~480 MB instead of ~145 MB). It is more
  accurate but about 2.5× slower on a CPU, so use it on a fast machine.
- No user service manager (for example, in a container)? Run `agent-sessions voice serve` in a
  terminal instead. It keeps the soft limits (2 threads, one request at a time, lower priority)
  but has no hard memory or CPU cap; `voice status` shows `"limits": "soft"`.
- `agent-sessions voice status` shows what is installed and running.
- `agent-sessions voice setup --rollback` goes back to the previous setup.
- `agent-sessions voice setup --rotate-token` issues a new access key. It refuses while a call is
  running.
- `agent-sessions voice remove` uninstalls. It asks first, refuses during a call, and keeps the
  models unless you add `--models`.

### Your own speech server

Any server that speaks the OpenAI audio API works:

- speech-to-text: `/v1/audio/transcriptions`;
- text-to-speech: `/v1/audio/speech`.

Self-hosted options include Speaches and Kokoro-FastAPI. Enter each side's base URL (ending in
`/v1`), a model, and an access key if the server needs one.

### OpenAI

Use `https://api.openai.com/v1` for both sides, with your API key. Pick models such as
`whisper-1` and `tts-1`. Calls then cost what OpenAI charges for audio.

**Test both** checks a pair of speech services before you save. It says what works and names what's
missing:

- "no voice list: enter the voice";
- "no streaming: first audio waits for the whole sentence";
- "no language detection: calls stay in the language you choose".

An access key is only ever sent to the address it was saved for. Change the address, and you must
enter that server's key again or remove it.

## On a call

- **Talk normally.** HQ waits until you've finished a thought before it answers; **Turn-taking**
  (Quick / Natural / Patient) sets how long it waits. Start talking while HQ speaks and it stops
  (turn **Interrupt HQ** off if your room is noisy).
- **Mute**, **Type** (say something by keyboard), **Captions** and **End** are on the call panel.
  The call bar follows you across the app.
- **A call ends when you end it:** hang up, or say anything that means you're done ("bye",
  "that's all", "tschüss"…). HQ judges that from what you mean, not from a word list, says
  goodbye and ends the call. A goodbye to a session ("say bye to the docs session") doesn't end
  the call, and neither does saying goodbye while a card still waits for your decision (HQ asks
  instead). If HQ's answer asks you something, or you start speaking again before it hangs up,
  the call goes on.
- **There is no time limit.** After 30 minutes without a sign of you (your words, or typing on
  the call) HQ asks "Are you still there, Commander?"; only no answer within about a
  minute ends the call.
- A call also ends when you sign out or when a speech service fails (the reason is shown).

### Acting by voice

When HQ proposes something it can do, such as a nudge, a new session or an answer to a Needs-you
question, it **reads the card back** and asks you to confirm:

> "A message to api-rebase: rerun the tests. Say confirm to send it, or cancel."

- Say **confirm** within 20 seconds of the read-back finishing. With several cards, say which one
  ("confirm the second").
- Anything else — silence, "cancel", talking over the read-back, or a new question — leaves the card
  untouched. "Confirm everything" is refused.
- Tapping the card works too, and always wins.
- Some operations need input on screen. Those are never offered by voice.

Spoken confirmation is **English-only**. On a call in another language, HQ reads the card and asks
you to confirm it on screen. If your speech server can tell languages apart, a "confirm" it heard
as another language is ignored, even on an English call.

## Voices and languages

Each General has their own voice, speaking style and speed. Defaults match their personality, and
you can change any of them in **Settings → AI → HQ calls → Each General's voice**:

- **Preview** speaks a fixed line in that General's voice.
- **Languages** gives a General a different voice per language. It is used while the call is in
  that language.
- A voice that your speech server no longer has is marked **Missing**. The General falls back to
  their own voice, or the Woman/Man voice, until you pick another.

Accents come only from voices that have them natively. HQ never imitates an accent in its text.

**Call language:**

- **Auto** follows the language you speak, if your speech server can tell. If it can't, calls are
  in English.
- Pick a fixed language to always use that one.

## Where calls don't work (yet)

- **Over Home Free.** Calls are refused when you reach BattleLab through [Home Free](/guide/home-free),
  with the reason shown, until the remote audio path has been measured.
- **Without HTTPS.** Browsers only give the microphone to secure pages.
- **One call at a time** per BattleLab.

## Privacy

- Audio goes from your browser to BattleLab, and from BattleLab to the speech services you chose.
  With **This machine**, that never leaves the machine.
- No audio is recorded or stored.
- What HQ says aloud is the same text it stores for the conversation, read aloud. The conversation
  appears in HQ like any typed one, marked as a call.
