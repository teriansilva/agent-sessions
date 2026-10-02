# Update & rollback

## The release layout is what makes both cheap

```
~/.local/share/agent-sessions/
├── releases/<ts>-<sha>/{src,venv}   one self-contained release per build
├── current → releases/<ts>-<sha>     atomic symlink; flipping it IS the upgrade
└── env                               0600; secret + admin hash + host/port/origin
```

Each release is an immutable directory with its own virtualenv. `current` is a symlink, so an
upgrade and a rollback are the same operation in opposite directions — a re-point, not a rebuild.
Re-running the installer is idempotent: it builds a new release, flips `current`, keeps the
previous releases (**3 by default**), and leaves your credentials untouched.

## Updating

Self-update never runs arbitrary input. It moves to the **channel's latest** release, flips
`current`, restarts, health-checks `/healthz`, and **rolls back automatically** if the new release
fails that check.

- **In-app** — Settings → System → Updates shows the running version with check/apply controls
  (`/api/version`, `/api/update/check`, `/api/update/apply`; authed, CSRF- and origin-gated).
- **CLI** — `agent-sessions autoupdate` checks the channel and applies only if newer.
- **Automatic** — an in-app toggle runs the same guarded check/apply daily. **Off by default.**

The **channel** selector switches between `stable` (the highest `v*` tag) and `main` (bleeding
edge). Both the channel and the auto-update toggle persist server-side and apply live — no
reinstall, no environment variable. Installs that used the old `AGENT_SESSIONS_AUTOUPDATE=1`
systemd timer migrate on upgrade: the opt-in is preserved as the in-app setting and the legacy
timer units are removed.

## Rolling back by hand

```sh
P=~/.local/share/agent-sessions
ls -1dt $P/releases/*/                       # newest first; pick the known-good one
# atomic re-point — temp-link + rename(2), the same idiom the installer uses
ln -s $P/releases/<ts>-<sha> $P/.current.rb && mv -Tf $P/.current.rb $P/current
systemctl --user restart agent-sessions.service
```

::: warning Do not use `ln -sfn`
`ln -sfn` unlinks before it relinks, so a concurrent start or health-check can observe a moment
with **no** `current` at all. The temp-link + `mv -Tf` above is a single `rename(2)`: an observer
sees either the old target or the new one, never nothing.
:::

A failed self-update or auto-update has already rolled itself back; the above is the manual path
for "the new release starts fine but behaves wrong".

## Emergency disable

```sh
systemctl --user stop    agent-sessions.service   # take it down now
systemctl --user disable agent-sessions.service   # …and keep it down across logins
journalctl --user -u agent-sessions.service -f    # logs
```

To stop **automatic updates only**, turn the toggle off under Settings → System → Updates, or set
`AGENT_SESSIONS_AUTOUPDATE=0` in `~/.local/share/agent-sessions/env`.

::: info Verified against
Commit `218cf3a` — `README.md § Updating / Rollback & emergency-disable`; `docs/reference.md § Install / update`.
:::
