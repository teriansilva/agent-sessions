# Handoff

Handoff moves a live piece of work from one engine to another — Claude Code to Codex, opencode to
Kimi, any direction — without you re-explaining the task.

The case is ordinary: one agent is stuck, or has burned its context, or the other one is simply
better at this. Handoff carries the thread across.

## How it works

BattleLab takes the **last few turns** of the source session, packs them into a seed, and delivers
that seed to the target engine as its opening input.

Two things make that harder than it sounds, and both are handled:

- **Readiness.** A freshly launched agent is not ready for input the moment its process exists;
  some engines advertise bracketed-paste support in their preamble and then still discard stdin for
  the better part of a minute. Delivery gates on the target having actually painted and gone quiet
  rather than on any single advertised capability.
- **Delivery.** The seed is sent as a bracketed paste, so a multi-line payload arrives as one block
  instead of being interpreted line-by-line as separate submissions.

## Handing off to an API session

An API client (`claude-api`, `codex-api`, `copilot-api`, `opencode-api`, `opencode2-api`) is a
handoff target too, and the dialog **prefers one**. It preselects, in order:

1. your default agent, as its API client when that client is ready;
2. the source's own agent as an API session (a terminal session moves to its API client);
3. the first ready API client;
4. the first other target.

An API session has no terminal, so nothing is pasted. BattleLab creates the session in the source's
project folder and sends the seed as its **first message**, exactly as if you had typed it there.
Nothing else can be sent to that session until the seed is in it, so it is always the first turn.

- **Permissions.** For an API target the dialog shows **Skip permission prompts**, starting at your
  stored default and unticked when that is unknown. A client that cannot skip prompts shows the row
  disabled with the reason. A session that skips prompts starts the moment the dialog closes, as
  New session starts one, and the seed is sent once it is ready.
- **Exactly one session, the seed at most once.** Pressing **Hand off** again for the same brief
  returns the same session; it never makes a second one or sends the seed twice. If the new
  session did not take the seed yet, the dialog stays open and says so; hand off again to retry.
  If the seed was refused after it was recorded, it shows in the session as an unsent message
  with **Send again**. If its delivery cannot be confirmed, it is never sent again on its own.
- **Archive.** "Archive current session after handoff" archives the source once the new session
  has the seed, not before, so a handoff that never landed leaves the source active.
- **Not across a restart.** A seed still waiting for its session is held in memory; a BattleLab
  restart before it is sent loses it, and the new session starts empty.

The model and reasoning level are the client's own defaults; change them in the session afterwards.

## Bounds

| Knob | Default |
|---|---|
| `AGENT_SESSIONS_HANDOFF_TURNS` | 6 turns of source conversation |
| `AGENT_SESSIONS_HANDOFF_CAP_BYTES` | 8192 bytes of seed (floor 256) |
| `AGENT_SESSIONS_HANDOFF_TTL_S` | 600 s handle lifetime |
| `AGENT_SESSIONS_HANDOFF_AI_INPUT_CHARS` | 24,000 characters considered for the AI summary |

When the seed does not fit, the payload is preferred over provenance: at most a quarter of the
budget goes to the "where this came from" header, leaving three quarters or more for the actual
content. Long paths are truncated in the middle with a marker rather than dropped.

## The AI-drafted variant

Handoff can send a model-drafted continuation instead of raw turns — a compact "here is the state,
here is what to do next" brief, bounded to 8 items and 800 characters of state. It uses the same
endpoint configuration as [AI review](/guide/ai-review), and is unavailable if that is not
configured.

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/handoff.py § SEED_MAX_TURNS, SEED_CAP_BYTES, HANDLE_TTL_S, AI_INPUT_CHARS, AI_STATE_MAX, AI_ITEMS_MAX, HEAD_PROVENANCE_FRACTION`.
API targets (#2141) — `src/agent_sessions/handoff_api.py`, `handoff.py § commit_api, api_blocks`, `plugins/kinds.py § API_HANDOFF_READY`, `web/src/lib/handoffTarget.ts`.
:::
