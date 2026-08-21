# Notifications

The bell tells you when a session wants you. It is a **recent-activity surface, not an archive**:
a bounded ring of at most **200** entries, oldest evicted.

Entries are capped at a 120-character title and a 240-character body — enough to say what happened,
short enough to scan.

## What actually announces

The distinction that matters: not every notification pushes. Only a session that has genuinely
**escalated** — one that is waiting on the operator — announces. Everything else accumulates
quietly in the list.

That restraint is the feature. A notification surface that fires on every state change trains you
to ignore it, at which point it is worse than not having one.

Notifications retire themselves when the thing they were about resolves: entries tied to an action
are cleared once that action reaches a terminal state, so the bell does not keep showing you a
question that has already been answered.

## Web push

You can subscribe the browser for push, so an escalation reaches your phone with BattleLab closed.
Subscription endpoints are validated before being accepted — an arbitrary URL cannot be registered
as a push target.

## Keeping the volume down

If you are getting too many, the causes are usually independent of each other and worth separating
rather than guessing between: how wide the detection window is, whether the same condition is being
re-announced, and whether duplicates are collapsing correctly. The stored notification records are
the place to measure that from — they show what actually fired, as opposed to what you remember
firing.

::: tip Deduplication is deliberate about what it keys on
A dedupe key never includes a model-authored field. Model text varies between runs for the same
underlying condition, so keying on it would make two announcements of one event look like two
events — the exact failure the dedupe exists to prevent.
:::

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/notifications.py § NOTIFY_MAX, TITLE_MAX, BODY_MAX, assert_pushable_endpoint, retire_for_actions`.
:::
