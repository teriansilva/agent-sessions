# OpenCode permission observations (#1339)

Checked against installed OpenCode **1.18.35**, 2026-10-09. Reproduce without credentials or
inference using `.venv/bin/python tests/fixtures/opencode_permissions_probe.py`. It uses an
isolated temporary HOME, the worker's actual agent-config helper, a loopback scripted model and
a harmless `printf`. The generated directory is retained for inspection.

Observed sequence: guarded prompt → one permission, answer native `always` → same command in
another prompt asks zero times → restart the process and load the same history → asks once
again. An explicitly pinned bypass agent asks zero times. The listener remains on loopback,
password-gated; the runtime's existing local-MCP password blanking is unchanged.

A local fixture MCP tool also confirms the reported progression: its approval has empty
`rawInput: {}`, while the preceding `in_progress` update has `rawInput: {repo: "agent-sessions"}`
with the same call id, title and `other` kind. Answering once reaches `end_turn`.

The native ACP request offers `once` / `always` / `reject`, but contains **no matching patterns**.
Its [`acp/permission.ts`](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/acp/permission.ts)
passes `permission.metadata` as `toolCall.rawInput`, dropping `patterns` and `always` from the
native permission event. MCP permission metadata can be empty even though the preceding running
tool update carries arguments. BattleLab now retains that bounded same-turn observation and
presents it separately as `toolContext`; it never changes the original callback or guesses input.

[`permission/index.ts`](https://github.com/anomalyco/opencode/blob/v1.18.35/packages/opencode/src/permission/index.ts)
adds every native `always` pattern under its permission name to the directory instance's
in-memory approved rules. It has no settings/database write. Those rules apply to later asks in
that instance, and disappear on instance disposal/worker restart. BattleLab runs one native
history per contained worker.

The always choice therefore discloses **unknown matching patterns, potentially all uses of a
tool**, worker/folder scope, no persistence, and reset on worker restart. It does not invent an
argv prefix or promise persistence for a resumed session. This follows the operator's approval
of every native proposed grant, including broad and risky ones, in
PR #1342 comment 89972 (linked in the implementation PR’s security review).
Server and worker independently require the exact proposed grant and complete request context.

The reported stuck session's journal showed `i:0` (`git remote -v`) answered and completed, then
`i:1` (`forgejo_list_repo_labels`) declined as unpresentable due to `{}` permission metadata.
The regression reproduces both callbacks through the worker. Separately, browser testing covers
a real queue bug: an outstanding decision previously caused clicks on other request cards to
return silently. Decisions now have independent request/turn/digest-bound state.
