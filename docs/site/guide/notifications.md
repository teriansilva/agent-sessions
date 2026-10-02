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

## Sessions that need you

For a session no mission holds, there is exactly **one** notification per time it starts needing
you, an *episode*. It is the same list as [Ask → Needs you](/guide/ask#needs-you): a session that
isn't on that list never notifies. The episode is remembered across restarts, so the same
situation is never announced twice, and clearing the bell doesn't bring it back.

When the session stops needing you (answered in its terminal, its decision settled, dismissed on
Ask, archived, or taken into a mission), the notification is **taken back**. The bell row goes on
the next check: straight away after a settlement, a dismissal or an archive, otherwise within a
minute. On a device you subscribed, the push is closed the next time BattleLab is open there, or when
the next notification arrives, whichever comes first. A push that arrives late, after its session
already stopped needing you, is checked with BattleLab first and closed straight away without a
sound. If the device can't reach BattleLab at that moment, the push is shown and closed later.
BattleLab never sends a push that only takes something back: browsers penalise pushes that show nothing, and would eventually replace them with
a generic notice of their own. Each episode's push carries its own tag, so taking one back never
closes another, and never closes a mission's own notifications. If the list can't be read, nothing
is taken back: unknown is not "nobody".

**One exception.** When BattleLab can't read which sessions a mission holds, it can't tell whether
a waiting session is yours or a mission's, so it announces it the way mission control does. That
notification's bell row clears when its decision settles, but its device notification isn't taken
back, just like a mission's own. Only that fallback behaves this way.

The row says what kind of stop it is (*Asks you to choose*, *Waiting for your approval*, *Asks you
a question*, *Stopped — take a look*). That text is fixed and never written by a model. Turn the
whole thing off in Settings → Session review → *Notify me when a session needs me*. Doing so takes
back any that are open. Sessions inside a mission are announced by mission control, as before.

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
Commit `218cf3a` — `src/agent_sessions/notifications.py § NOTIFY_MAX, TITLE_MAX, BODY_MAX, assert_pushable_endpoint, retire_for_actions`; #1086 Phase 4 — `notifications.py § sync_needs_you, KIND_REASON, episode_tag`, `needs_you_notify.py`, `webpush.py § build_payload, CLOSE_MAX`, `web/src/swPush.ts`.
:::
