# Terminal

The terminal pane is BattleLab's own: an `xterm.js` front end over a WebSocket
(`WS /ws/term/{sid}`) to a `dtach` PTY. It is built mobile-first, because the case it exists for is
checking on an agent from a phone.

## The compose bar

Phone keyboards and raw PTYs disagree about almost everything: autocorrect fires mid-command, there
is no <kbd>Ctrl</kbd>, and a stray newline submits a half-written prompt. So typing happens in a
**compose bar** — an ordinary text field you edit freely, that sends only when you say so.

Its default state is a preference: `auto`, `open` or `collapsed`.

Above it sits a control-key row for the keys a soft keyboard has no way to produce —
<kbd>Ctrl-C</kbd>, <kbd>Esc</kbd>, arrows, <kbd>Tab</kbd> — sent as real terminal input rather than
simulated keystrokes.

## Images and files

You can paste or drop an image straight into the compose bar; `POST /api/upload` saves it to the
shared uploads directory and the path goes to the agent, which is what the agent can actually act
on. Pasting a screenshot of a stack trace to Claude Code works the way you would want it to.

::: warning Clipboard access varies by browser
Paste is the one interaction whose failure is entirely client-side — a browser that denies
clipboard access produces no server request at all. If a paste appears to do nothing, the absence
of any `/api/upload` request in the logs is the diagnostic: the browser refused before BattleLab
was involved.
:::

## Scrolling

Scrolling is more subtle than it looks, because some engines take over the mouse themselves. When
an engine has armed mouse tracking, the terminal forwards wheel events to it and the *engine*
scrolls — that is why scroll behaviour differs between, say, opencode and Antigravity. When it has
not, BattleLab scrolls its own buffer.

Engines also differ on which screen buffer they render into. Recent Claude Code runs in the
alternate screen buffer, and that changes what "scroll up" and drag-selection mean. BattleLab reads
the terminal's actual live state rather than assuming a per-engine constant, because the answer has
changed under it before.

A scroll-to-bottom control appears when you are not pinned to the live tail.

## Selection and copy

Dragging to select pins the buffer for the duration of the drag, so output arriving mid-selection
cannot yank the text out from under your finger.

## Taking over a live session

`AGENT_SESSIONS_TAKEOVER` enables single-active-viewer take-over: a second viewer of a live session
can explicitly claim it rather than silently competing for input with the first. This sits on top
of the single-writer lock, which is about *processes*; take-over is about *people*.

::: info Verified against
Commit `218cf3a` — `docs/reference.md § Terminal`; `src/agent_sessions/ptybridge.py`; `web/src/components/terminal/`; `src/agent_sessions/prefs.py § COMPOSE_DEFAULTS`.
:::
