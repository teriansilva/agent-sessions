# AI review

AI review reads your running sessions and tells you which ones need attention — the one that hit an
error, the one waiting on a question, the one that quietly finished twenty minutes ago.

It is **off until you configure it**, and it works against any OpenAI-compatible endpoint. That
includes a local model: point it at your own server and no session content leaves your host.

## Configuration

Under Settings → AI review:

| Key | Meaning |
|---|---|
| `enabled` | Master toggle. |
| `base_url` · `api_key` · `model` | Any OpenAI-compatible endpoint. |
| `interval_minutes` | How often the background loop runs. |
| `max_input_chars` | Ceiling on the input built per session. |
| `request_timeout` | 10–600 s. Takes precedence over `AGENT_SESSIONS_AI_REVIEW_TIMEOUT`. |
| `prompt` | The review prompt, editable. |

The default timeout is **120 seconds** — deliberately generous, because it is sized for slow local
models rather than a hosted API. The model-list call (`/models`) keeps its own much shorter budget
of 10 seconds, with a 60-second cache, so a mis-typed URL fails the picker fast instead of hanging
the settings page.

`AGENT_SESSIONS_AI_REVIEW_LOOP=0` is a hard kill-switch: the background task is never started,
overriding the Settings toggle. Manual **Review now** is unaffected — which is the point of having
both.

## What gets sent

The reviewer builds its input from two sources: the session's **transcript** and its **live
screen**. It errors only when *both* are empty.

That is not an implementation detail, it is why a [plain shell](/guide/engines) can be reviewed at
all — a shell has no transcript store, so it is reviewed on its screen alone, with no special case
anywhere in the review code.

Output is bounded: a 200-character summary, a 120-character title, a 280-character reason. Roughly
4000 characters of the live screen tail are considered.

## Recaps

The same machinery produces a per-session **recap** — a longer summary, capped at 1500 characters
from up to 16,000 characters of input. Recaps feed [Pulse](/guide/pulse): at `fast` depth Pulse
does no model work of its own and ranks entirely from these, which is what makes the default depth
free.

## What it costs

One completion per reviewed session per interval, plus whatever Pulse adds at `medium`/`slow`.
With a hosted API that is a real bill; with a local model it is your own GPU. `interval_minutes`
and the loop kill-switch are the two dials that matter.

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/review.py § SUMMARY_MAX, TITLE_MAX, REASON_MAX, LIVE_TAIL_CHARS, RECAP_MAX, RECAP_INPUT_CHARS, MODELS_TIMEOUT_S, MODELS_CACHE_TTL_S`; `docs/reference.md § AGENT_SESSIONS_AI_REVIEW_TIMEOUT, _AI_REVIEW_LOOP`.
:::
