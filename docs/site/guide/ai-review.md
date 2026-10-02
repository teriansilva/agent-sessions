# AI review

AI review reads your running sessions and tells you which ones need attention — the one that hit an
error, the one waiting on a question, the one that quietly finished twenty minutes ago.

It is **off until you configure it**, and it works against any OpenAI-compatible endpoint. That
includes a local model: point it at your own server and no session content leaves your host.

## Configuration

AI review is set up on two Settings pages under **AI**, and its prompt lives in a third:

| Setting | Where | Key |
|---|---|---|
| Base URL · key | Endpoint & model → Connection | `base_url` · `api_key` |
| Model · request timeout | Endpoint & model → Model | `model` · `request_timeout` (10–600 s) |
| Periodic review on or off | Session review | `enabled` |
| How often it runs | Session review | `interval_minutes` |
| Sessions excluded from review | Session review (exclude one from its row actions) | — |
| The review prompt | Prompts → Tail review | `prompt` |

The endpoint is shared: the same connection serves recaps, handoff briefs, auto-sort, the
mission-control scan, the orchestrator and mission turns. `max_input_chars`, the ceiling on the input
built per session, is a stored preference in the same block with no control of its own.

`request_timeout` takes precedence over `AGENT_SESSIONS_AI_REVIEW_TIMEOUT`. The default timeout is
**120 seconds** — deliberately generous, because it is sized for slow local models rather than a
hosted API. The model-list call (`/models`) keeps its own much shorter budget of 10 seconds, with a
60-second cache, so a mis-typed URL fails the picker fast instead of hanging the settings page.

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
from up to 16,000 characters of input. Recaps feed [mission control](/guide/missions): the `slow`
session scan writes each session's one-line state from its recap when there is one, and the session
finder searches recap text as well as summaries.

The default `fast` scan does no model work of its own — it ranks sessions from the review results
above, which is what makes it free.

## What it costs

One completion per reviewed session per interval, and only for sessions with new activity since
their last review. A successful review can make a **second** call to refresh the session's
chronological recap — skipped by its own fingerprint when the whole session has not changed since
the last recap. On top of that comes whatever the other AI features you turn on add, such as up to
12 calls per `slow` session scan. With a hosted API that is a real bill; with a local model it is your own GPU.
`interval_minutes` and the loop kill-switch are the two dials that matter.

::: info Verified against
Commit `8b1c66b` — `src/agent_sessions/review.py § SUMMARY_MAX, TITLE_MAX, REASON_MAX, LIVE_TAIL_CHARS, RECAP_MAX, RECAP_INPUT_CHARS, MODELS_TIMEOUT_S, MODELS_CACHE_TTL_S`; `src/agent_sessions/pulse.py § SLOW_SESSION_CAP, build_cards, _synthesize_sessions`; `src/agent_sessions/pulse_chat.py § _card_haystack`; `web/src/routes/AiEndpointSetup.tsx`, `web/src/routes/AiReviewSettings.tsx`; `docs/reference.md § AGENT_SESSIONS_AI_REVIEW_TIMEOUT, _AI_REVIEW_LOOP`.
:::
