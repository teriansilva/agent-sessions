# `claude_select_menu.screen.txt`

A claude multiple-choice prompt (the `AskUserQuestion` select list), **as the app's own screen
renderer sees it** — the input `orchestrator.screen_options` parses (#1060).

- **Captured** 2026-09-22 from a real claude session (`2.1.280 (Claude Code)`) that had stopped at
  this menu. The session's raw scrollback bytes were replayed through `vtscreen.render` at the
  width the ring was authored at (190 columns), starting from the last absolute cursor position
  before the menu — claude paints with relative moves, so a replay from mid-frame lands the cursor
  in the wrong place.
- **Changed**: the words only. The title, the question and the four option labels and
  descriptions were rewritten to neutral text. Every piece of chrome and geometry is byte-for-byte
  the capture's: the `☐` title line, the blank line, the `│` question line, the `❯` on the
  selected option, the two-space option indent, the five-space wrapped description indent, the
  `Type something.` / separator / `Chat about this` tail, the blank line, and the footer.
- **Why it matters**: the parser is deliberately stricter than `_prompt_class`. It accepts only
  this rendering, anchored on the footer as the last line of the screen, so a numbered list printed
  anywhere else — prose, logs, an echoed menu — never becomes a button.

# `claude_select_menu_v2_1_280.screen.txt`

The same kind of menu from a **fresh** claude `2.1.280` session, captured 2026-09-23 with
`tmux capture-pane` at 120×40, the screen exactly as drawn. The session was started in an empty
scratch folder and asked to call `AskUserQuestion` with three colour options, so the words are
neutral as captured and **nothing was changed**.

- **What it adds**: this rendering draws the question **without** the `│` gutter, and the title with
  a leading space. The first fixture has the gutter at the same version, so the parser accepts both.
- **What it proved** (#1060 Phase 3): in a single-question menu the **digit alone submits**. Typing
  `2` answered "Green" at once, with no Enter, so an Enter sent after the digit lands in the next
  prompt.
