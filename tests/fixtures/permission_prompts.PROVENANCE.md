# Tool-permission dialog captures (`*_permission_*.raw`, #1213)

Raw PTY bytes, replayed by the tests through `vtscreen.render_cells` exactly as
`scrollback.live_tail_frame` would. Raw rather than rendered text because opencode draws its cursor
**only as a background colour**, which a text render drops.

**Changed in every file:** two host names, replaced byte-for-byte with strings of the same length so
no cursor position moves — the operator's account name (`operator-01`) in claude's paths, and the
model provider's domain (`example-ai`) in opencode's status line. Nothing else.

## opencode 1.18.33 (65 × 46)

- `opencode_permission_grep.raw` — **the #1213 incident's own ring**
  (`opencode:new-3cfd4de6-…`, mission `msn_59b131df…`), cut at the start of the escape sequence that follows the first complete frame showing
  `△ Permission required · ✱ Grep "(?i)mission" · Pattern: (?i)mission` with the cursor on
  *Allow once*. The mission launched opencode with BattleLab's unattended agent
  (`engines/opencode.py` `UNATTENDED_PERMISSION`: `grep` asks).
- `opencode_permission_shell_truncated.raw` — the same ring, whole. It ends partway through
  drawing the second dialog (`# Shell command`, `$ git log …`): no option row, no footer. **Must
  parse to nothing** — a half-drawn dialog is not a dialog.
- `opencode_permission_shell.raw` — a fresh opencode in a private PTY in an empty scratch folder,
  launched with the same unattended-agent config, asked to run `mkdir -p probe-dir-1213`. Cursor on
  *Allow once*.
- `opencode_permission_shell_cursor2.raw` — the same session after one `→`: identical text, cursor
  on *Allow always*. The text grid of this file and the previous one is the same; only colour moved.
- `opencode_permission_always_stage.raw` — the same session after `Enter` on *Allow always*:
  opencode's own second stage, `△ Always allow` listing `- mkdir *`, cursor on *Confirm*.

**Keys proven on these sessions (2026-09-28):** `Enter` = the cursor's option (the incident);
`→ Enter` opened the Always-allow stage and `Enter` there granted it (the command ran);
`→→ Enter` in ONE write rejected (nothing ran); `→ Enter` in one write then `Enter` also worked;
`Esc` rejected.

## claude 2.1.284 (100 × 40)

A fresh `claude --model haiku --permission-mode default` in a private PTY in an empty scratch
folder, asked to run `mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt` (Bash) or to create
`notes-1213.txt` (Write).

- `claude_permission_bash.raw` — `Bash command`, cursor (`❯`) on `1. Yes`.
- `claude_permission_bash_cursor2.raw` — after one `↓`: cursor on `2. Yes, and don't ask again …`,
  and the footer loses its `· Tab to amend` half.
- `claude_permission_write.raw` — `Create file` with its `╌╌╌`-framed preview and
  `Do you want to create notes-1213.txt?`.
- `claude_permission_answered.raw` — after `↓↓ Enter`: the dialog is gone
  (`Interrupted · What should Claude do instead?`). **Must parse to nothing.**

**Keys proven (2026-09-28):** the digit ALONE selects and submits — `1` ran the command, `3`
answered No, no Enter needed (an Enter after it would land in the next prompt). `↓↓ Enter` also
answered No; `Esc` rejected the Write.
