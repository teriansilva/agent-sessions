# `claude_ask_user_question.json`

Claude Code's `AskUserQuestion` callback as BattleLab's Claude API client receives it (#1463).

- **Captured** 2026-10-15 from `2.1.300 (Claude Code)` run with the client's own flags
  (`--print --permission-prompts host --input-format stream-json --output-format stream-json
  --permission-prompt-tool stdio`), asked to use the tool with two options. The frame is verbatim.
  It arrived **identically under `--permission-mode bypassPermissions` and `default`**: a question
  needs the user (`requires_user_interaction: true`), so skipping permissions does not skip it.
- **Answers, observed in the same runs:** `allow` with the original input plus
  `answers: {"Do you prefer red or blue?": "blue"}` produced the tool result *"The user answered:
  "Do you prefer red or blue?"="blue". …"*; `allow` with the original input alone (what a plain
  Approve sends) produced *"The user did not answer the questions."*
