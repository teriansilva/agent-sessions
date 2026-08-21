# Settings

Settings is server-side state, not browser state: preferences live in
`~/.config/agent-sessions/prefs.json` (override with `AGENT_SESSIONS_PREFS`), so they follow you
between the desktop and the phone. That file is deliberately separate from the install root's `env`
— reinstalling replaces releases, not your preferences.

## Appearance

- **Theme** — `dark` (default) or `light`.
- **Accent** — the brand amber (`#ffb000`) is user-customizable. Everything derived from it (soft
  and glow variants, the CTA surface, the ink that sits on it) recomputes from your choice, so a
  custom accent recolours the whole app coherently instead of leaving amber fragments behind.
  Status colours are **not** derived from the accent and never change with it: a status must never
  be confusable with a brand colour.
- **Compose default** — whether the compose bar starts `auto`, `open` or `collapsed`.
- **Session list order** — by recent activity (default) or creation time.

## Projects

Project entities, their colours and folders, plus the three visibility controls described in
[Projects](/guide/projects) — `projects_hidden`, `folder_exclusions` and `projects_mode`. They are
easy to confuse and do genuinely different things; that page has the table.

## AI review

Endpoint, model, interval, input ceiling, request timeout and the prompt itself. See
[AI review](/guide/ai-review).

## Pulse

Auto-refresh, interval, window in days, and the scan depth that decides what Pulse costs. See
[Pulse](/guide/pulse).

## System → Updates

The running version, a check/apply control, the `stable`/`main` channel selector and the
automatic-update toggle. Both the channel and the toggle persist server-side and apply live — no
reinstall, no environment variable. See [Update & rollback](/start/update).

## Security

The 2FA enrollment flow, and the change-password form. See [First login](/start/first-login).

::: tip A setting that appears not to save
Preferences are written server-side immediately. If a panel appears to lose a value on remount, the
cause is a stale client-side copy of the configuration rather than a failed write — the value is on
disk. Reloading the page shows the true state.
:::

::: info Verified against
Commit `218cf3a` — `src/agent_sessions/prefs.py § THEMES, DEFAULT_ACCENT, COMPOSE_DEFAULTS, SESSION_LIST_ORDERS, PROJECT_MODES`; `docs/reference.md § POST /api/prefs`.
:::
