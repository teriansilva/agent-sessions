# Calling HQ

You can **call HQ** and talk to it like a phone call. HQ answers out loud in the voice of the General you
called, you can interrupt it mid-sentence, and it can act on what you say: send a nudge, start a
session, answer a Needs-you question. Everything happens through the same [HQ](/guide/hq) you type
to, with the same tools, limits and confirmations.

The call runs **in the browser**: your microphone, BattleLab, and a speech server you choose. It is
not a phone line, and no audio is stored anywhere.

## Set it up

Calls need two speech services: speech-to-text, which hears you, and text-to-speech, which speaks
HQ's answers. You configure them in **Settings → AI → HQ → Calls**. You have three options.

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
- `--runtime-only` installs just the call runtime, with no local speech server or its models. Point
  HQ calls at a speech server you already run; **Use this machine** needs
  `agent-sessions voice setup --server-only` (the speech server alone).
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
  (Quick / Natural / Patient) sets how long a pause it allows: none, about 1.5 and 2.5 seconds.
  HQ already starts working on your words during that pause, so a longer setting rarely makes it
  answer later. If you pick up again before the pause is over, the rest joins the same question. Start talking while HQ speaks and it stops
  (turn **Interrupt HQ** off if your room is noisy).
- **Trail off and HQ waits.** If you stop in the middle of a thought, HQ gives you about three
  seconds before it answers on Natural and Patient; a finished sentence is answered after the
  setting's pause. Quick answers at once either way.
- **HQ answers like a person.** Right after you ask, it usually says a short "Hmm, let me see."
  (German: "Hm, Moment mal.") while it works on the answer, so you know it heard you. It talks
  the way people talk on the phone: "Okay, so…", contractions, now and then an "uh" or a quick
  correction. Card texts and anything it reads back for you to confirm stay word for word. The
  "Hmm" is recorded once in each voice early in the call, so the very first question of a call
  (or of a new language) may come without it.
- **HQ thinks out loud while it works.** On a longer question it says what it is doing in its own
  words as it goes ("Okay, I'm pulling up the issue list in Forgejo." … "Hm, it's not in the open
  ones. Let me try the closed issues.") instead of a stock "Still on it". If the model writes
  nothing, the stock lines stay.
- **Talk to HQ while it works.** Ask "what are you doing?" and it tells you and carries on. Give
  it a hint ("it's issue 2059") and it uses it in what it is doing. Ask something else and it
  says it will finish this first, answers, then takes your new question. Say "forget that, do X"
  and it drops what it was doing and does X. If HQ cannot tell what you meant it says "One sec,
  I'll finish this first." and your words wait their turn. When words that waited get their
  turn, HQ knows you said them a while ago: it says what it is answering ("About your question
  on staging earlier: …"), or says nothing if they no longer need an answer (a "no hurry", or a
  question its last answer already settled). The thread then shows "Nothing to add".
- **Say "stop" to stop what HQ is doing.** While HQ works on an answer the call screen shows
  what it is doing and a **Stop** button above the call controls. Tapping it, or saying nothing
  but "stop" ("cancel that", "never mind"; in German "stopp" or "abbrechen"), stops that answer;
  HQ says "Stopped." and keeps listening. It never ends the call, and a sentence that only
  contains the word ("stop the staging deploy") is a request like any other.
- **Ask the next thing while HQ is still working.** A question you ask while HQ works on
  another is queued behind it, in order, like one you typed (up to three; after that HQ says it
  already has questions waiting). "Stop" stops only what HQ is working on: the next question
  then starts. Say **"stop everything"** ("cancel everything"; in German "alles stoppen") to
  stop that and take back everything waiting. Each answer is spoken in turn. If a question you
  spoke was waiting behind something you typed and you have asked a newer one since, HQ only
  says "I have that answer now." for the older one and its answer is on screen: ask for it if
  you want to hear it. If you hang up, questions still waiting run anyway and their answers are
  in the conversation.
- **Ask to see something and it opens.** "Open the automations", "pull up the api-rebase
  session in the map": HQ puts it on the screen of the device you are calling from
  ([Showing it on screen](/guide/hq#showing-it-on-screen)). On a phone the call panel covers the
  page, so the call shrinks to its bar under the top bar, with **Mute**, **Open** (back to the
  call panel) and **End**; on a wide screen the panel stays where it is.
- **Captions are off unless you turn them on** (Settings → AI → HQ → Calls → Conversation, or the
  call panel). A call then shows only the cards that need your decision.
- **Mute**, **Type** (say something by keyboard), **Captions** and **End** are on the call panel.
  The call bar follows you across the app; its one caption line quotes the newest HQ line that
  has finished playing, never one still queued or cut off by an interruption.
- **A call ends when you end it:** hang up, or say anything that means you're done ("bye",
  "that's all", "tschüss"…). HQ judges that from what you mean, not from a word list, says
  goodbye and ends the call. A goodbye to a session ("say bye to the docs session") doesn't end
  the call, and neither does saying goodbye while a card still waits for your decision (HQ asks
  instead). If HQ's answer asks you something, or you start speaking again before it hangs up,
  the call goes on.
- **There is no time limit.** After 30 minutes without a sign of you (your words, or typing on
  the call) HQ asks "Are you still there?"; only no answer within about a
  minute ends the call.
- A call also ends when you sign out or when a speech service fails (the reason is shown).
- **The screen stays on during a call**, where the browser allows it. A phone stops listening
  once its screen goes dark, so the call asks the browser to keep the screen awake until you hang
  up (a battery saver can refuse). If you lock the phone yourself or switch to another app, HQ
  may not hear you until you come back.
- **A slow speech server pauses rather than stutters.** If text-to-speech can't keep up, HQ waits
  a moment and then speaks the next stretch in one piece.
- Choose the call's model and thinking in **Settings → AI → HQ → HQ models → Calls**.
  Thinking defaults to **Off** so HQ answers promptly; **Inherit** follows **Endpoint & model**.
  **Auto · provider policy** is a separate choice. The model also inherits by default, or you
  can select another model on the same endpoint. These fields do not follow a typed-only
  choice. A nested question-routing or mission-conversation request can have its own choice;
  each field's source label explains the fallback. Speech and voice controls remain in Calls.
- **HQ speaks a reply in whole sentences** when your speech server sends audio while it is still
  generating it, so the voice stays the same from the first word to the last. On a server that
  sends each sentence only when it is finished, HQ still starts with a short first piece, so you
  hear it sooner.

### Acting by voice

When HQ proposes something it can do, such as a new session or an answer to a Needs-you question,
it **reads the card back** and asks you to confirm:

> "A new session in api: rerun the tests. Say confirm to start it, or cancel."

A message you tell HQ to send into one of your sessions is not read back: HQ sends it right away
and tells you whether it arrived.

- **A long brief is not read out.** If the brief for a new session is longer than a couple of
  sentences, HQ says what would start and where, and that the brief is on your screen ("A new
  Claude Code session in api. Its brief is too long to read out; it is on your screen."). You
  can still say **confirm**: you are then starting a brief you have not heard in full. Messages
  to a session and changes HQ proposes are always read in full, or stay on screen.
- Say **confirm** within 20 seconds of the read-back finishing. With several cards, say which one
  ("confirm the second").
- Say **dismiss it** to throw the card away (with several cards, "dismiss the second"). Nothing of
  it runs. This works on HQ's own cards: sessions, changes, Library drafts and messages.
- Anything else — silence, "cancel", talking over the read-back, or a new question — leaves the card
  untouched. "Confirm everything" is refused.
- To **change** a card, just say what to change ("call it Game Place Promotions instead"). HQ puts
  up the corrected card, reads it back ("Changed: …"), and the old one is marked **Replaced**.
- When a card can't be confirmed by voice, HQ says why (for example, a new project whose folder
  doesn't exist yet). Confirm those on screen.
- Tapping the card works too, and always wins.
- Some operations need input on screen. Those are never offered by voice.

Spoken confirmation is **English-only**. On a call in another language, HQ reads the card and asks
you to confirm it on screen. If your speech server can tell languages apart, a "confirm" it heard
as another language is ignored, even on an English call.

## Voices and languages

Each General has their own voice, speaking style and speed. Defaults match their personality, and
you can change any of them in **Settings → AI → HQ → Calls → Each General's voice**.

By default each General speaks with a voice of their own whenever your speech server has it:

- on the bundled speech server (Kokoro), each General gets a different English voice;
- a server with a voice named after the General (for example `ileana` or `mateo`) uses that one.

Otherwise the General uses the Woman or Man voice. After adding voices to your speech server,
press **Read list** and then **Save** in the HQ calls settings, so BattleLab sees the new list.

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
