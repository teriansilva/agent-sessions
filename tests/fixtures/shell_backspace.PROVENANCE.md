# `shell_backspace_*` and `claude_permission_bash_backspace_*` (#2058)

The rings the backspace model in `vtscreen` is tested against, each with the screen a reference
terminal showed for the same bytes (`<name>.tmux.txt`), so the tests need no terminal at run time.
All of them are written by `shell_backspace_record.py` in this folder:

    python3 tests/fixtures/shell_backspace_record.py <output dir> --tmux

- **Recorded** 2026-11-24 on Linux 6.8 (a kernel PTY), `GNU bash, version 5.2.21(1)-release`,
  `tmux 3.4`. Re-running the recorder on the same host reproduced every `.raw` and `.tmux.txt`
  file byte for byte.
- **The shell rings** (`shell_backspace_edit`, `_edit_dumb`, `_col0`, `_wrap`, `_margin`) are
  every byte a real `/bin/bash --norc --noprofile -i` wrote to a private PTY of a fixed geometry
  while the recorder typed at it one key at a time, Backspace sending DEL as a keyboard does. The
  shell runs under `env -i` with only `PS1='$ '`, `TERM` (`xterm`, or `dumb` for `_edit_dumb`) and
  `PATH` set, in `/`, so no host, user or path name is in any byte. **Nothing was changed.**
  What they show: the line editor erases with `BS SP BS` on both terminal types; a program's BS
  stops at column 0; to erase back across a wrapped line the editor moves up itself (`CSI A`)
  rather than relying on reverse wrap; and when a typed line reaches the last column the editor
  forces the wrap (`SP CR`), so it never emits BS in the pending-wrap state.
- **The adversarial rings** (`claude_permission_bash_backspace_swap`, `_command`) are the
  checked-in `claude_permission_bash.raw` followed by a short **synthetic** tail
  (`ADVERSARIAL` in the recorder) that uses backspaces to rewrite option labels or the command
  in place. The tail is written by hand on purpose, it is the attack; what is recorded is the
  screen tmux showed for the result, which the renderer must match row for row.
- **The reference** is tmux 3.4 (`capture-pane -p`, trailing blank rows dropped), fed the bytes
  raw (`stty raw -echo; cat`) at the ring's geometry on a private server socket.

## BS in the pending-autowrap state is not pinned

#2058 names xterm with default resources as the reference for BS after a character was written
in the last column. There is no xterm on the recording host, so none of those cases is pinned and
the renderer refuses all of them (`_Screen.backspace`). For the record, tmux 3.4 on a 5 x 3 screen
(the recorder's `--tmux` output, not asserted anywhere):

| Bytes | tmux 3.4 shows |
|---|---|
| `abcdeX` | `abcde` / `X` |
| `abcde BS X` | `abcdX` |
| `abcde BS BS X` | `abcXe` |
| `abcde BS×7 X` | `Xbcde` |
| `abcde BS X Y` | `abcdX` / `Y` |
| `abcde BS SP BS Z` | `abcdZ` |

That is, tmux cancels the pending wrap and leaves the cursor on the last cell on the first BS.
Terminals are not known to agree on this, which is why it is not taken from tmux. To trust any of these cases, record xterm's
answer for it here first and model exactly that.
