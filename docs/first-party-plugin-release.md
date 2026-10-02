# First-party plugin release evidence (#1266)

This Linux x64 matrix pins release inputs and records the actual vendor smoke results below.
No package lifecycle script is executed. Exact artifact SHA-256 digests, destinations and
runtime closures are in [`release/recipes/linux-x64`](../release/recipes/linux-x64/).

| Agent | Vendor distribution | Artifacts | Compressed bytes | Largest member | Sign-in | Probe | Live result |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| claude | [@anthropic-ai/claude-code-linux-x64@2.1.286](https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.286.tgz) | 1 | 108247300 | 241667256 | `auth-login` | `print-pinned` | Install/version pass; vendor weekly quota refused conversation |
| opencode | [opencode-linux-x64-baseline@1.18.34](https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.18.34.tgz) | 1 | 60309539 | 185632896 | `auth-login` | `run-session` | All required checks passed |
| codex | [@openai/codex@0.159.3-linux-x64](https://registry.npmjs.org/@openai/codex/-/codex-0.159.3-linux-x64.tgz) | 1 | 162464331 | 287086056 | `cli-subcommand` | `exec-readonly` | All required checks passed |
| gemini | [@google/gemini-cli@0.62.0](https://registry.npmjs.org/@google/gemini-cli/-/gemini-cli-0.62.0.tgz) | 1 | 20787241 | 16697694 | `interactive` | `prompt-pinned` | Install/version/trust flow pass; vendor account rejects this client |
| kimi | [@moonshot-ai/kimi-code@2.1.1](https://registry.npmjs.org/@moonshot-ai/kimi-code/-/kimi-code-2.1.1.tgz) | 31 | 18927599 | 21988585 | `cli-subcommand` | `prompt-session` | All required checks passed |
| antigravity | [@google-antigravity/antigravity-cli@1.2.14](https://github.com/google-antigravity/antigravity-cli/releases/download/1.2.14/agy_cli_linux_x64.tar.gz) | 1 | 60882033 | 220655824 | `interactive` | `print-conversation` | All required checks passed |

npm distributions were also checked against publisher SHA-512 metadata. Kimi has 31 required
runtime packages; optional native terminal/clipboard packages are omitted. Its real new/resume,
transcript and usage checks passed with that exact closure. Node 22.23.2 is present on the smoke host; Gemini requires
Node 20+, Kimi 22.19+. Other platforms must be refused before artifact work.

Codex's inspected native executable is 287,086,056 bytes, exceeding the 128 MiB member cap.
The 320 MiB cap fits this cut while retaining the 512 MiB expansion and 256 MiB
compressed bounds per archive (1 GiB total expansion per install). The Antigravity download took
454.7 seconds on this host; the download shares the install's existing 600-second total deadline.
No per-artifact timeout extends that total budget.

## Fixed command shapes to validate

`MESSAGE` is a server-authored nonce challenge; `ID` passes the manifest's validator. These
are closed kinds, not arbitrary argv supplied by a manifest or request.

| Kind | New conversation | Resume |
| --- | --- | --- |
| `print-pinned` | `--print --tools "" --disable-slash-commands --session-id ID MESSAGE` | Replace `--session-id` with `--resume` |
| `exec-readonly` | `exec --sandbox read-only --skip-git-repo-check --json MESSAGE` | Same parent options, then `resume ID --json MESSAGE` |
| `run-session` | `run --pure --format json MESSAGE` | Add `--session ID` |
| `prompt-pinned` | `--approval-mode default --session-id ID --prompt MESSAGE` | Replace `--session-id` with `--resume` |
| `prompt-session` | `--prompt MESSAGE` | Add `--session ID` |
| `print-conversation` | `--print MESSAGE --disable-slash-commands --print-timeout 60s` | Add `--conversation ID` |

Sign-in selects `auth login`, `login`, or an interactive entrypoint. Sign-in and verification use
the same candidate workspace, so a vendor's explicit trust choice applies to the later check.
The app never supplies that answer. `run-session` also supplies a newly minted primary-agent
profile denying every tool, overriding that CLI's permissive defaults without reusing a project
agent name. No kind grants permission bypass. CLI help and the vendors' [Claude](https://code.claude.com/docs/en/cli-reference),
[Gemini](https://geminicli.com/docs/cli/headless/), [OpenCode](https://opencode.ai/docs/cli/) and
[Antigravity](https://antigravity.google/docs/cli/headless/) references informed these shapes.
The machine-readable [smoke evidence](../release/evidence/linux-x64-2026-10-01.json) records the
actual executable digests and checks. Installs used cached publisher downloads through the real
installer pipeline and an isolated test signing identity, not the production release key.
Private homes held minimal copies of existing account configuration, without copying or rewriting
operator transcripts. Vendor checks created history and consumed quota. Claude reported its
weekly limit (reset October 4, 17:00 Europe/Bucharest). Gemini passed the explicit Trust folder
choice through the actual sign-in transport, then its vendor rejected authentication with
`UNSUPPORTED_CLIENT` for Gemini Code Assist for individuals. Neither refusal is a successful
conversation check; both candidates remain disabled. Kimi and Gemini require a compatible Node runtime on
the host; this cut was exercised with Node 22.23.2.

## Acceptance and signed cut

- Record each recipe/executable digest, host, actual version, sign-in behavior and every required
  check. Use isolated homes/workspaces, never seed or rewrite the operator's real stores. Record
  vendor effects without credentials/raw output. Unsupported mode, authentication failure, quota
  exhaustion and timeout are separate failures, never passing evidence.
- Shell remains the built-in bash exception, outside the feed. API chat remains a built-in,
  artifact-free smoke case with an operator endpoint and per-file write consent.
- Record the reviewed main commit and rehearse a complete unsigned feed/release snapshot. Record
  the intended tag, feed sequence, whole-feed SHA-256 and expiry, and release snapshot digest.
- Unlock the primary signing identity only for that concrete cut. The recovery key is not used
  routinely. Sign the feed under `battlelab-plugins` and verify both feed and release identities
  against the installed trust root before publishing.
- Publish `plugin-feed.json` and `plugin-feed.json.sig` together on the corresponding GitHub
  `teriansilva/agent-sessions` release, verify public bytes, then publish the verified Forgejo
  signed tag last to start deployment. Verify the deployed trust root, gallery refresh via
  `POST /api/plugins/feed/refresh`, install flow, existing attachments and API chat before closing.
- Marcus owns renewal before the seven-day expiry: advance sequence and timestamps over reviewed
  recipes, unlock primary, sign/verify the pair, update release assets, and verify deployed refresh.
  Record the next concrete deadline with the successful cut. No unattended signer is added.

The separate #982 marketing release hold is unchanged.

## v0.19.3 cut record

The first signed application and plugin-feed cut was published on 2026-10-02 after
PR #1269: Hermes review
5613 approved head `50357f46d114f990ba3ad4e4450b24e4a86eaa72`, all eight CI checks passed
(including 7,955 backend tests), and the reviewed pristine install/uninstall smoke passed.

| Evidence | Value |
| --- | --- |
| Private release | `v0.19.3` |
| Reviewed Forgejo commit | `6c1988aa13993c6d3730a8447c35b78fd17bac00` |
| Public release | [v0.19.3](https://github.com/teriansilva/agent-sessions/releases/tag/v0.19.3) |
| Deterministic public snapshot | `866d601a68ba47cc14d8287fd98e199fe7592c3f` |
| Feed sequence | `1` |
| Feed SHA-256 | `c3d9ab631e12ca859bd68c205c2bb82528ff94e3b011e88d57cff53243a43ffc` |
| Issued | 2026-10-02 11:47:15 UTC |
| Expires | 2026-10-09 11:47:15 UTC (14:47:15 Europe/Bucharest) |
| Signer | Primary `SHA256:2aBGF8oP1PEvJFiD2/GWVLnl8sC3IJ2lhNahZjm/reY` |

Both annotated release tags and the feed signature were verified against the reviewed trust
root. The complete feed/signature pair was published before the deploy-triggering private tag;
the application's anonymous feed client verified the actual public bytes. The recovery key
was not used. Forgejo's `v*` protection requires the operator account for the final push.

**Renewal owner: Marcus, before 2026-10-09 11:47:15 UTC.** Publish a complete primary-signed
pair with a strictly greater sequence and fresh timestamps, then verify deployed refresh.
Expiry refuses new installations; it does not disable installed agents. No scheduled or
unattended signer was introduced.

**Retention boundary observed during this cut.** A separate in-app update to merged main
ran the previously installed `2787342` installer at 15:08–15:10 EEST. That older installer
did not understand `retain-releases` and pruned `20261001-083800-4aa08e4`. The two other
original rollback directories retained their identities, and the existing live session
survived. The retention marker only protects updates run by an installer containing the
hold support; it cannot change an older installer's code. The signed deployment used the
reviewed installer and retained every directory present after that separate update.

Production reported version `0.19.3` at the reviewed commit after deploy run 8887. The signed
catalog refresh matched the feed digest and all six recipes; the eight-agent roster and API-chat
configuration matched the pre-cut baseline. Desktop and mobile browsers opened the gallery
and signed installation review without horizontal overflow or page errors. These checks only
created review records, not installations or activations. A receive-only attachment to the
existing Claude session preserved its processes, socket and terminal geometry without sending
input or resize frames. Vendor verification outcomes remain those recorded in the matrix above.

Release-notes run 8889 stopped with exit 141 when `git log | head -200` closed a long changelog
pipe. The documented manual fallback published the private notes after the shared tag verifier
and reviewed-main identity checks passed. The follow-up changes the producer to `git log -200`,
which emits the identical 200 lines without SIGPIPE; it changes no tag or signature guard.
