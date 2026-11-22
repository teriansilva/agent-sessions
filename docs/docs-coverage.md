# Docs coverage ledger (#2249)

Where the manual (`docs/site`) documents each part of BattleLab. HQ answers "how do I…?" from
the manual, so a part with no page is a part HQ can't explain. `tests/test_docs_coverage.py`
checks this file against the code, not against its own text.

**What the test guarantees.** *Inventory*, nothing more:

- every navigation entry in `web/src/components/shell/sections.ts` (each of `SECTIONS` **and its
  `children`**, `HQ_VIEWS` included) has a row here;
- every `####` capability group in [`hq-coverage.md`](hq-coverage.md)'s ledger has a row here;
- every page a row names exists and is in the manual's sidebar, and every published page is in
  the sidebar (redirect stubs with `search: false` excepted);
- no row names an entry, a group or a page that no longer exists.

**What it doesn't.** It does not tie individual routes or prefs to a page, and it can't tell
whether a page is *right*. A change inside an area that already has a page is held by the review
rule in [`CONTRIBUTING.md`](../CONTRIBUTING.md#docs-are-part-of-done) instead: a user-visible
change updates its page in the same PR.

**Row format.** `<key>  ->  <page>[, <page>…]` or `<key>  ->  gap #<issue>`. A page is a path
under `docs/site` without `.md` (`guide/hq`), or a directory index with a trailing slash
(`reference/`). A `gap` row names the open issue that will write the page. Its shape is checked
offline; whether the issue is still open is checked in review.

## Navigation

Keys are `section:<id>` / `sub:<id>`, the ids in `sections.ts`. The label follows as a comment.

```docs-nav
section:ask          ->  guide/dashboard                  # Dashboard
section:mission      ->  guide/hq                         # HQ
sub:hq               ->  guide/hq                         # HQ → Conversations
sub:missions         ->  guide/missions                   # HQ → Missions
sub:dispatches       ->  guide/notifications              # HQ → Notifications
sub:projects         ->  guide/projects                   # HQ → Projects
section:sessions     ->  guide/sessions                   # Sessions
sub:sessions         ->  guide/sessions, guide/terminal   # Sessions → Sessions
sub:map              ->  gap #2250                        # Sessions → Sessions map
section:library      ->  guide/templates                  # Library (opens Prompt Templates)
sub:templates        ->  guide/templates                  # Library → Prompt Templates
sub:variables        ->  guide/templates, guide/secrets   # Library → Variables
sub:mcp              ->  guide/mcp-servers                # Library → MCP servers
sub:memory           ->  guide/memory                     # Library → Memory
sub:artifacts        ->  gap #2250                        # Library → Artifacts
sub:automations      ->  guide/automations                # Library → Automations
sub:webhooks         ->  guide/webhooks                   # Library → Webhooks
sub:skills           ->  gap #2250                        # Library → Skills
sub:specialists      ->  guide/specialists                # Library → Specialists
sub:playbooks        ->  guide/playbooks, guide/gallery   # Library → Playbooks
sub:mission-plans    ->  guide/mission-plans              # Library → Mission plans
```

## HQ capability groups

Keys are the `####` headings of [`hq-coverage.md`](hq-coverage.md)'s ledger, verbatim.

```docs-groups
Sessions (sidebar, row menu, pane)                            ->  guide/sessions, guide/terminal
New session and projects                                      ->  guide/engines, guide/projects
Missions                                                      ->  guide/missions
Decisions, Needs you, notifications, Recent work, dashboard   ->  guide/dashboard, guide/hq, guide/notifications
Settings                                                      ->  guide/settings
Library                                                       ->  guide/templates, guide/playbooks, guide/specialists, guide/automations, guide/memory, guide/mcp-servers
Files and git (Files drawer)                                  ->  guide/files-and-git
Help and the manual                                           ->  guide/hq
API-only routes (no UI caller today)                          ->  reference/
```
