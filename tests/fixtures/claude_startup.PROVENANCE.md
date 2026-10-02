# claude startup captures (#966)

What a freshly dispatched `claude --session-id <uuid>` writes to the server-owned headless reader
before anyone types. Nothing was ever written to either session.

| file | bytes |
|---|---|
| `claude_startup_80x24.bin` | 1435 |
| `claude_startup_80x24.chunks.json` | 12 reads (`{offset, length, t_ms}`, `t_ms` from reader spawn) |
| `claude_startup_120x40.bin` | 1690 |
| `claude_startup_120x40.chunks.json` | 12 reads |

## How they were recorded

- **claude:** `2.1.272 (Claude Code)`. This is the binary the service pins (`AGENT_SESSIONS_CLAUDE_BIN` = `~/.local/bin/claude`).
- **Recorded:** 80x24 at `2026-09-15T04:22:24Z`, 120x40 at `2026-09-15T04:22:58Z`. The runs were back to back, not concurrent.
- **cwd:** a trusted repository folder (`hasTrustDialogAccepted: true`). No trust state was changed.
- **Launch (master):** `dtach -n <sock> -z -E -r winch <claude-bin> --session-id <fresh uuid>`. The argv comes from `ClaudeProvider.new_launch_argv(bypass=False)` (`mission_dispatch` passes `bypass=False`) wrapped by `ptybridge.launch_argv(detached=True)`. stdin/stdout/stderr are DEVNULL, with `start_new_session` and `close_fds`.
- **Environment:** the running service's variable set. Every `CLAUDE*` name and every shell-only name (`TERM`, `COLORTERM`, `PWD`, `AI_AGENT`, …) is dropped, and the service `PATH` is used. Then `TERM=xterm-256color` and `COLORTERM=truecolor` are set, as the dispatcher's `setdefault` does. The dispatcher itself strips nothing; the service simply has no `CLAUDE*` variables.
- **Reader:** the socket was polled every 50 ms, as `dispatch` does (it appeared about 4 ms after launch). Then `ptybridge.attach_argv` (`dtach -a <sock> -z -E -r winch`) ran in an `os.openpty()` pair sized with `TIOCSWINSZ` **before** the spawn, which is `session_stream.SessionStream.start`. The drain was `os.read(fd, 65536)` (`_READ_CHUNK`).
- **Stop rule:** at least 20 s after the first byte **and** 5 s of silence after 2004 was armed. Both runs ended that way, after about 20.0 s.
- **Deviations** (none change the bytes claude writes):
  - the socket path is shortened to fit AF_UNIX's 108-byte limit;
  - no `systemd-run --scope` wrapper;
  - no flock fd passed;
  - no `engine_auth` `claude -p` preflight.
- **Byte 0–5 is not claude.** `ESC[H ESC[J` arrives 1.7 ms after the reader spawns. It is dtach's own attach clear (the literal is in the dtach binary). It lands in the production ring too, so it stays in the fixture.

**To re-record:** reproduce the steps above by hand (the one-off harness that recorded these was not committed), then apply the same-length redaction below. `tests/test_headless_seed.py::test_a_REAL_claude_boot_opens_the_headless_gate_without_typing` (opt-in, `AGENT_SESSIONS_TEST_REAL_CLAUDE=1`) drives the same production launch and reader path against the installed binary, so it fails first when claude's startup screen changes. Afterwards, terminate the reader, the dtach master and the claude process group: SIGTERM, then SIGKILL after 3 s, never `killpg` a pgid ≤ 1. Remove the socket.

## Timeline (identical sequence at both sizes)

| read | 80x24 t_ms | 120x40 t_ms | bytes | content |
|---|---|---|---|---|
| 0 | 1.7 | 1.8 | 6 | dtach attach clear `ESC[H ESC[J` |
| 1 | 689 | 732 | 13 | `ESC7 ESC[r ESC8 ESC[?25h` |
| 2–3 | 773 | 845 | 30 | `?25l`, **`?2004h`** @25, `?2031h`, `?1004h` |
| 4 | 832 | 879 | 12 | queries `ESC[>0q ESC[?u ESC[c` (nobody answers on this path) |
| — | *476 ms gap* | ***2996 ms gap*** | | |
| 5 | 1308 | 3875 | 33 | `ESC[>4m ESC[<u`, `?1004l ?2031l` **`?2004l`** @86 |
| 6 | 1539 | 4130 | 24 | **`?2004h`** @94 (re-armed), `?2031h ?1004h` |
| 7 | 1551 | 4143 | 53 | **`?1049h`** @118, `ESC[2J ESC[H`, mouse `?1000h ?1002h ?1003h ?1006h`, `?25l` |
| 8 | 1561 | 4153 | 20 | OSC 0 title |
| 9 | 1624 | 4221 | 1189 / 1447 | **the whole first paint, in one read** |
| 10 | 1875 | 4489 | 12 / 8 | queries again |
| — | *9.7 s gap* | *9.7 s gap* | | |
| 11 | 11602 | 14169 | 43 / 44 | repaint erasing the `◉ xhigh · /effort` hint (9 → 8 rows) |

No `?1049h` or `?2004h` straddles a read boundary in either capture.

## Measured

| | 80x24 | 120x40 |
|---|---|---|
| total bytes | **1435** | **1690** |
| ring when 2004 first armed (end of read 3) | 49 | 49 |
| first *armed + quiet ≥1 s* | 1392 B, quiet from 2875 ms | **61 B, quiet from 1879 ms** (pre-paint, 0 rows, primary screen) |
| *armed + painted + quiet ≥1 s* | 1392 B, quiet from 2875 ms | 1646 B, quiet from 5489 ms |
| non-blank `vtscreen.render` rows, whole capture | 8 | 8 |
| rows at armed + painted + quiet | 9 | 9 |
| rows at first armed + quiet | 9 | 0 |
| rows in every state before read 9 | 0 | 0 |
| alt screen active at end (`scrollback._in_alt_screen`) | yes | yes |
| today's gate (armed ∧ ≥2048 B ∧ quiet), replayed on the 250 ms poll grid | **never opens** (max 1435 B, 613 short) | **never opens** (max 1690 B, 358 short) |
| `alt ∧ rows ≥ 4` gate, same replay | opens at 3002 ms, 1392 B | opens at 5502 ms, 1646 B; **closed** through the 61 B window |

The 8 rows at the end are: three logo/model/cwd lines, a login-expiry warning, a rule, the prompt, a rule, and the mode line. The warning is account state and will not always be there, so the structural floor is **7**. The row count does not grow with terminal height; it is 9 then 8 at both 24 and 40 rows.

## Notes for the fix

- **`vtscreen.render` does not model the alternate screen.** `CSI ? … h/l` is skipped wholesale, so primary text survives `?1049h` and alt text survives `?1049l`. For these captures this is harmless: nothing printable precedes `?1049h`, and claude clears with `ESC[2J ESC[H` right after it. `render(data[118:])` gives the same 8 rows as `render(data)`. Pair the row count with `_in_alt_screen`.
- **A blank alt-screen boot renders 0 rows.** Both `ESC[?1049h` and `ESC[?1049h ESC[?2004h ESC[H ESC[2J` give 0 at both sizes.
- **Chunking is irrelevant to both signals.** Each capture was fed through `scrollback._buffer_append` in 44 ways, against a temp `AGENT_SESSIONS_SCROLLBACK_DIR`:
  - the recorded reads;
  - 1-byte reads;
  - a cut at every interior byte of each `?2004h`/`?1049h`;
  - all of them cut at once;
  - 20 random splits.

  All 44 gave a byte-identical ring, `has_mode(key, 2004)` True, the same tracked mode set `{1000, 1002, 1003, 1006, 2004}`, and an identical `render`.
- **Unanswered terminal queries.** Claude sends XTVERSION, the kitty keyboard query and DA1, and nothing answers on the headless path. The stall after them was 0.5 s in one run and 3.0 s in the other; two samples cannot tell a query timeout from ordinary startup work.
- **Content is account-specific, so the committed files are redacted.** See below.

## Redaction

The raw captures carried account state. Before committing, it was replaced with **same-length ASCII for ASCII** substitutions. Every escape byte, every multi-byte glyph and every read boundary is untouched.

| where (both sizes) | recorded | committed |
|---|---|---|
| model line | the model name and effort level | the same text with every letter and digit replaced by `X` |
| plan name | the plan | `XXXXXX XXX` |
| login-expiry warning | the warning text | `Account notice text redacted · notice text removed` |
| effort hint (`◉ …`, erased by read 11) | the effort level | `XXXXX` |
| the random `Try "…"` tip | the tip | `example prompt` padded with `.` |

Re-verified on the committed files:
- **Total length:** 1435 B (80×24) and 1690 B (120×40), unchanged. 103 and 116 bytes differ, all printable ASCII on both sides.
- **Escape offsets:** every `ESC` is at the same offset. `?2004h` @25, `?2004l` @86, `?2004h` @94 and `?1049h` @118 at both sizes.
- **Rows:** the non-blank `vtscreen.render` row count is identical at **every byte prefix**, and the per-row display widths are identical at every read boundary. The counts are 8 for the whole capture, 9 at armed + painted + quiet (1392 B / 1646 B), 0 at 61 B and 0 in every state before read 9.
- **Denylist:** no public-snapshot denylist hits.
