# Uninstall

```sh
curl -fsSL https://battlelab.superstatus.io/uninstall.sh | sh
```

`uninstall.sh` removes every artifact `install.sh` creates. That contract is not a promise in prose
— it is a CI gate: `pr-validate` fails the build if the installer creates something the uninstaller
does not remove, so the two cannot drift apart.

## What it removes

- The install root, `~/.local/share/agent-sessions/` — every release directory, the `current`
  symlink, and the `env` file holding your secret key and password hash.
- The `systemctl --user` unit at `~/.config/systemd/user/agent-sessions.service` (and any legacy
  update timer units left by older installs).

## What it does not touch

**Your agents' own data.** Claude Code's `~/.claude/`, Codex's `~/.codex/`, opencode's database,
Gemini's `~/.gemini/`, Kimi's `~/.kimi-code/` — BattleLab reads those stores, and for every engine
except Claude it never writes to them at all. Uninstalling the organizer does not remove the
conversations it was organizing.

Your working directories are untouched too. BattleLab never owned them; it launched agents inside
them.

If you want the sidecar state gone as well — titles, project assignments, favourites, archive flags,
preferences — remove `~/.config/agent-sessions/`. That directory is BattleLab's own, separate from
the install root, and deliberately survives a reinstall.

::: info Verified against
Commit `218cf3a` — `uninstall.sh`; `install.sh`; `.forgejo/workflows/pr-validate.yml § installer ↔ uninstaller contract`.
:::
