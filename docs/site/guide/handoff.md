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
:::
