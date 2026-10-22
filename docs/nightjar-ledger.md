# NIGHTJAR coverage ledger (#1531)

Every stylesheet, popup component, canvas and page BattleLab ships, with its NIGHTJAR status. It
exists because a rework that relies on memory forgets areas; this one fails the build instead.
The rules are `docs/design.md` v2; the enforcement is `web/src/theme/nightjarLedger.test.ts`.

**Columns**

- **Path:** relative to `web/src/` for `css` / `popup` / `canvas` rows; a repo path or a named
  surface for `page` rows.
- **Kind:** `css` (a stylesheet), `popup` (a component rendering `role="dialog|menu|listbox|alertdialog"`,
  `aria-modal`, `<dialog` or `createPortal`), `canvas`, `page`.
- **Phase:** the #1531 phase that reskins it.
- **Status:** `todo` · `done` · `n/a: <reason>`.
- **Captures:** for a `done` row, the sweep capture ids that show it in each of its states
  (`gallery:<section>` for the primitives gallery, `route:<id>` / `popup:<id>` for the sweep).
- **Motion:** for a `done` row, the catalog entries it uses, or `none (reason)`.

**What the test enforces**

1. Every `*.css` under `web/src` and every popup component has a row (a new file without one fails).
   Every `css` / `popup` / `canvas` row names a file that exists.
2. A `done` stylesheet has no literal colour (`#hex`, `rgb()`, `hsl()`), no non-zero
   `border-radius` outside LED / radar / round rules, no `box-shadow` with a blur that is not a
   `--shadow-*` / `--glow-*` token, no numeric `z-index`, and no `font-size` that is not a
   `--fs-*` token. Comments are stripped first. Keyword colours (`black`, `white`) inside a
   `color-mix()` are allowed: they are mixing anchors, not palette colours.
3. A `done` row names its captures and its motion.
4. **Reviewers add rows, never just tick them.** A state found in review that has no row gets a row.

**Literal allowlist** (content palettes keep their published values; they are not chrome):
`tokens.css` (the palette itself), `theme/termSchemes.ts`, `theme/apiLook.ts`, the `--syn-*`
syntax colours (in `tokens.css`), and the Appearance previews of other schemes. The test reads
the same list.

| Path | Kind | Phase | Status | Captures | Motion |
|---|---|---|---|---|---|
| `app/App.css` | css | 4 | done | route:dashboard route:new-session route:boot popup:session-row-menu | sector fade-in (Full), boot power-on, ≤800px drawer slide-in + backdrop |
| `components/agents/Roster.module.css` | css | 11 | todo |  |  |
| `components/agents/SessionDefaults.module.css` | css | 11 | todo |  |  |
| `components/ask/AskCards.module.css` | css | 4 | done | popup:hq | card deploy-in, LED change flash |
| `components/ask/AskConsole.module.css` | css | 4 | done | popup:hq | answer word fade + caret, match-row arrival, portrait scan (Full, ambient), acquire bar |
| `components/ask/AskHome.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring route:dashboard-recent popup:needs-you-details popup:recent-work | rows arrive (slide + tint flash), needs-you LEDs blink in Full (steady in Reduced/Off), first read Acquiring (scan bar) |
| `components/ask/AskSidebar.module.css` | css | 4 | done | popup:hq | drawer slide-in + scrim (≤800px) |
| `components/automations/automations.module.css` | css | 10 | todo |  |  |
| `components/chat/ChatPane.module.css` | css | 6 | todo |  |  |
| `components/chat/EditProposal.module.css` | css | 6 | todo |  |  |
| `components/dashboard/Dashboard.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring route:dashboard-unread route:dashboard-recent | readouts flash on change (filter step), LEDs flash on a state change, new rows arrive (slide + tint flash), first read Acquiring (radar / scan bar), re-read keeps its data under the refresh bar (scan, static in Reduced/Off), Recent work grows its row (grid-rows) |
| `components/EnableLoginDetails.module.css` | css | 11 | todo |  |  |
| `components/files/filePanel.module.css` | css | 7 | todo |  |  |
| `components/files/mediaPreview.module.css` | css | 7 | todo |  |  |
| `components/FolderPickerModal.module.css` | css | 3 | done | popup:folder-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/gallery/gallery.module.css` | css | 10 | todo |  |  |
| `components/gallery/viewTabs.module.css` | css | 10 | todo |  |  |
| `components/hq/HqContactGallery.module.css` | css | 11 | todo |  |  |
| `components/hq/hq.module.css` | css | 4 | done | popup:hq | portrait loop (Full only, paused with the ambient pause) |
| `components/hq/PrimaryContact.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring | plate edge lights on hover (no movement); the portrait's own loop is HqPortrait's |
| `components/hud/buttonGlitch.css` | css | 2 | done | gallery:buttons | glitch: filter only |
| `components/HudDialog.module.css` | css | 3 | done | popup:hand-off popup:move-to-project popup:session-brief | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/hud/nightjar.css` | css | 2 | done | gallery:all | atmosphere, power-on, rail pulse, LED breathe/blink/flash, pips, gauge pulse, readout flash, seg/tab marker, disclosure, loaders, arrival, stream |
| `components/links/HandoffStub.module.css` | css | 4 | done | popup:handoff-stub | none (a static notice) |
| `components/links/OpenLinkDialog.module.css` | css | 3 | done | popup:open-link | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/memory/agentAccess.module.css` | css | 10 | todo |  |  |
| `components/memory/importCard.module.css` | css | 10 | todo |  |  |
| `components/memory/memoryGraph.module.css` | css | 10 | todo |  |  |
| `components/newSession/NewSessionControls.module.css` | css | 6 | todo |  |  |
| `components/overview/overview.css` | css | 9 | todo |  |  |
| `components/overview/sessionWindow.module.css` | css | 9 | todo |  |  |
| `components/overview/windowLayer.module.css` | css | 9 | todo |  |  |
| `components/playbooks/playbookCanvas.module.css` | css | 10 | todo |  |  |
| `components/playbooks/playbookEditor.module.css` | css | 10 | todo |  |  |
| `components/playbooks/playbookPrompts.module.css` | css | 10 | todo |  |  |
| `components/playbooks/playbooks.module.css` | css | 10 | todo |  |  |
| `components/playbooks/ProjectPlaybook.module.css` | css | 10 | todo |  |  |
| `components/plugins/PluginSetup.module.css` | css | 11 | todo |  |  |
| `components/ProjectColorPicker.module.css` | css | 10 | todo |  |  |
| `components/projects/ProjectSessionDefaults.module.css` | css | 10 | todo |  |  |
| `components/pulse/direction.module.css` | css | 8 | todo |  |  |
| `components/pulse/missionDrawer.module.css` | css | 8 | todo |  |  |
| `components/pulse/mission.module.css` | css | 8 | todo |  |  |
| `components/pulse/missionThread.module.css` | css | 8 | todo |  |  |
| `components/pulse/NotificationBell.module.css` | css | 4 | done | popup:bell | deploy-in, ≤800px drawer slide-in + scrim, row arrival flash, badge bump (filter) |
| `components/pulse/Orchestrator.module.css` | css | 8 | todo |  |  |
| `components/sessions/AdoptToMissionModal.module.css` | css | 3 | done | popup:adopt | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/settings/AgentEndpointCard.module.css` | css | 11 | todo |  |  |
| `components/settings/ResourceLimits.module.css` | css | 11 | todo |  |  |
| `components/shares/Shares.module.css` | css | 11 | todo |  |  |
| `components/shell/HelpMenu.module.css` | css | 4 | done | popup:operator-menu | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/shell/OperatorMenu.module.css` | css | 4 | done | popup:operator-menu | tile hover transition |
| `components/shell/SectionNav.module.css` | css | 4 | done | route:dashboard route:mission | tab tint transition |
| `components/sidebar/Filters.module.css` | css | 6 | done | route:sidebar route:sidebar-folded route:sidebar-empty | disclosure (grid rows 0fr → 1fr, one container), chevron turn, segment tint transition |
| `components/sidebar/RowMenu.module.css` | css | 3 | done | popup:session-row-menu | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/sidebar/SessionList.module.css` | css | 6 | done | route:sidebar route:sidebar-empty route:sidebar-error route:sidebar-rename popup:session-row-menu | row arrival (nj-arrive + nj-flash overlay, LED nj-led-flash; Reduced: the single fade), FLIP reorder, review spin (nj-spin, Full only), toast-in, marquee (Full only) |
| `components/slideshow/Slideshow.module.css` | css | 4 | done | popup:tour popup:whats-new | none (static dots) |
| `components/structured/BackgroundWork.module.css` | css | 6 | todo |  |  |
| `components/structured/Markdown.module.css` | css | 6 | todo |  |  |
| `components/structured/NotSent.module.css` | css | 6 | todo |  |  |
| `components/structured/Outbox.module.css` | css | 6 | todo |  |  |
| `components/structured/Questions.module.css` | css | 6 | todo |  |  |
| `components/structured/StructuredPane.module.css` | css | 6 | todo |  |  |
| `components/templates/ConfirmDialog.module.css` | css | 3 | done | popup:confirm popup:git-confirm popup:playbook-delete | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/templates/SuggestionsPanel.module.css` | css | 10 | todo |  |  |
| `components/templates/TemplatePickerModal.module.css` | css | 3 | done | popup:template-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/Compose.module.css` | css | 6 | done | route:session-compose route:session-pane popup:keybar | Talk recording: mic glyph opacity step (Full only), key menu appear / ≤800px sheet-up, Send glitch (.shine) |
| `components/terminal/HandoffModal.module.css` | css | 3 | done | popup:hand-off | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/RuntimeGate.module.css` | css | 6 | todo |  |  |
| `components/terminal/SentMessagesModal.module.css` | css | 3 | done | popup:sent-messages | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/SessionRecapModal.module.css` | css | 3 | done | popup:session-brief | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/Terminal.module.css` | css | 6 | done | route:session-pane popup:head-menu popup:toast | head chip glitch on press (nj-glitch-step, Full only), connect-chip radar (nj-radar), history pill steps (nj-steps), copied toast opacity only — nothing over the terminal moves |
| `components/ui/actionButton.module.css` | css | 2 | done | gallery:buttons | press glitch (filter), hover |
| `components/updates/UpdateProgressPanel.module.css` | css | 11 | todo |  |  |
| `components/updates/UpdateWhatsNew.module.css` | css | 11 | todo |  |  |
| `components/wizard/WizardShell.module.css` | css | 4 | done | route:new-project | progress fill, rail tint |
| `index.css` | css | 1 | todo |  |  |
| `routes/AgentsSettings.module.css` | css | 11 | todo |  |  |
| `routes/Appearance.module.css` | css | 11 | todo |  |  |
| `routes/Ask.module.css` | css | 4 | done | popup:hq | none (layout) |
| `routes/Checklists.module.css` | css | 8 | todo |  |  |
| `routes/Memory.module.css` | css | 10 | todo |  |  |
| `routes/NewProject.module.css` | css | 10 | todo |  |  |
| `routes/NewSessionLanding.module.css` | css | 6 | todo |  |  |
| `routes/NightjarGallery.module.css` | css | 2 | done | gallery:all | none (layout) |
| `routes/Onboarding.module.css` | css | 4 | done | popup:tour | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `routes/ProjectsManager.module.css` | css | 11 | todo |  |  |
| `routes/Pulse.module.css` | css | 8 | todo |  |  |
| `routes/Settings.module.css` | css | 11 | todo |  |  |
| `routes/Templates.module.css` | css | 10 | todo |  |  |
| `routes/Variables.module.css` | css | 10 | todo |  |  |
| `share/ShareViewer.module.css` | css | 12 | todo |  |  |
| `theme/fonts.css` | css | 1 | done | gallery:all | none (font faces) |
| `theme/motion.css` | css | 1 | done | gallery:all | the mode rules and keyframes themselves |
| `tokens.css` | css | n/a | n/a: the palette itself (literal allowlist) | — | — |
| `whatsnew/WhatsNewDialog.module.css` | css | 4 | done | popup:whats-new | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced; slide image fade |
| `components/ui/menu.module.css` | css | 3 | done | popup:operator-menu popup:session-row-menu popup:dashboard-new | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/ui/toast.module.css` | css | 3 | done | popup:toast | toast in (fade + 6 px rise), out at 120 ms |
| `components/pulse/MissionTitle.module.css` | css | 8 | todo |  |  |
| `components/shell/FooterTelemetry.module.css` | css | 4 | done | route:dashboard | value bump (filter step, Full only) |
| `components/shell/BootSequence.module.css` | css | 4 | done | route:boot | boot: log lines fade in, acquire bar, layer fades out; panels power on (fade + rise) |
| `app/ShellStates.module.css` | css | 4 | done | route:loading route:error | loader acquire bar (Full), failed panel deploy-in |
| `app/App.tsx` | popup | 4 | done | popup:session-row-menu | ≤800px drawer slide-in + backdrop |
| `components/ask/AskSidebar.tsx` | popup | 4 | done | popup:hq | drawer slide-in + scrim (≤800px) |
| `components/ask/AskSwitcher.tsx` | popup | 4 | done | popup:hq | listbox deploy-in |
| `components/ask/NeedsYouDetailsDialog.tsx` | popup | 3 | done | popup:needs-you-details | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/ask/RecentWork.tsx` | popup | 3 | done | popup:recent-work | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/automations/ConsentDialog.tsx` | popup | 3 | done | popup:consent | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/files/BranchMenu.tsx` | popup | 3 | done | popup:git-branch-menu | appear (fade, no rise), row tint transition (anchored on a phone too, design.md §5) |
| `components/files/FilePanel.tsx` | popup | 7 | todo |  |  |
| `components/files/FileViewerModal.tsx` | popup | 3 | done | popup:file-viewer | deploy-in (fade + 6 px rise + scan), scrim fade |
| `components/files/GitTab.tsx` | popup | 3 | done | popup:git-confirm | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/files/RowMenu.tsx` | popup | 3 | done | popup:git-row-menu | appear (fade, no rise), row tint transition (anchored on a phone too, design.md §5) |
| `components/files/UploadControl.tsx` | popup | 3 | done | popup:upload-menu | appear (fade, no rise), row tint transition (anchored on a phone too, design.md §5) |
| `components/files/UploadQueue.tsx` | popup | 3 | done | popup:upload-collision | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/FolderPickerModal.tsx` | popup | 3 | done | popup:folder-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/links/OpenLinkDialog.tsx` | popup | 3 | done | popup:open-link | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/newSession/NewSessionControls.tsx` | popup | 6 | todo |  |  |
| `components/overview/SessionTextDialog.tsx` | popup | 3 | done | popup:session-text | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/playbooks/PlaybookDialog.tsx` | popup | 3 | done | popup:playbook-delete | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/MissionConsole.tsx` | popup | 8 | todo |  |  |
| `components/pulse/MissionOverflow.tsx` | popup | 3 | done | popup:mission-overflow | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/pulse/MissionRail.tsx` | popup | 8 | todo |  |  |
| `components/pulse/MissionTitle.tsx` | popup | 3 | done | popup:mission-title | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/NotificationBell.tsx` | popup | 4 | done | popup:bell | deploy-in, drawer slide-in, row arrival flash |
| `components/pulse/ObjectiveDirectionDialog.tsx` | popup | 3 | done | popup:objective-direction | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sessions/AdoptToMissionModal.tsx` | popup | 3 | done | popup:adopt | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/shares/SharePubliclyModal.tsx` | popup | 3 | done | popup:share-publicly | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sidebar/MoveToProjectModal.tsx` | popup | 3 | done | popup:move-to-project | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sidebar/RowMenu.tsx` | popup | 3 | done | popup:session-row-menu | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/structured/PermissionsDialog.tsx` | popup | 3 | done | popup:permissions | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/structured/StructuredPane.tsx` | popup | 6 | todo |  |  |
| `components/templates/ConfirmDialog.tsx` | popup | 3 | done | popup:confirm popup:git-confirm | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/templates/SessionChooserModal.tsx` | popup | 3 | done | popup:session-chooser | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/templates/TemplatePickerModal.tsx` | popup | 3 | done | popup:template-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/Compose.tsx` | popup | 6 | done | popup:sent-messages popup:template-picker popup:keybar | deploy-in (the dialogs it mounts), appear (its KeyBar menu) |
| `components/terminal/HandoffModal.tsx` | popup | 3 | done | popup:hand-off | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/HeadActions.tsx` | popup | 3 | done | popup:head-menu | appear (fade, no rise), row tint transition (anchored on a phone too, design.md §5) |
| `components/terminal/KeyBar.tsx` | popup | 3 | done | popup:keybar | appear (fade, no rise), row tint transition (anchored on a phone too, design.md §5) |
| `components/terminal/SentMessagesModal.tsx` | popup | 3 | done | popup:sent-messages | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/SessionRecapModal.tsx` | popup | 3 | done | popup:session-brief | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/Terminal.tsx` | popup | 6 | done | popup:head-menu popup:toast popup:session-brief popup:hand-off route:session-pane | appear (head menu), toast (opacity only over the terminal), deploy-in (the dialogs it mounts) |
| `components/ui/AnchoredMenu.tsx` | popup | 3 | done | popup:operator-menu popup:dashboard-new | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `routes/NewSessionLanding.tsx` | popup | 6 | todo |  |  |
| `routes/Onboarding.tsx` | popup | 4 | done | popup:tour | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `routes/RenameProjectModal.tsx` | popup | 3 | done | popup:rename-project | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `whatsnew/WhatsNewDialog.tsx` | popup | 4 | done | popup:whats-new | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/MissionHeaderActions.tsx` | popup | 3 | done | popup:unarchive popup:mission-overflow | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/overview/OverviewCanvas.tsx (React Flow)` | canvas | 9 | todo |  |  |
| `components/memory/MemoryGraph.tsx (React Flow)` | canvas | 9 | todo |  |  |
| `components/playbooks/PlaybookFlowCanvas.tsx (React Flow)` | canvas | 9 | todo |  |  |
| `web/index.html (SPA shell, pre-paint script)` | page | 1 | todo |  |  |
| `src/agent_sessions/templates/login.html` | page | 12 | todo |  |  |
| `src/agent_sessions/templates/login_totp.html` | page | 12 | todo |  |  |
| `src/agent_sessions/templates/change_password.html` | page | 12 | todo |  |  |
| `src/agent_sessions/templates/link_approve.html` | page | 12 | todo |  |  |
| `web/share.html → share/ShareViewer.tsx` | page | 12 | todo |  |  |
| `src/agent_sessions/routes/shares.py PLACEHOLDER_PAGE` | page | 12 | todo |  |  |
| `web/connect.html (+ homefree/connectState.ts)` | page | 12 | todo |  |  |
| `landing/index.html` | page | 12b | n/a: rebuilt in NIGHTJAR by #1544 (PR #1548); 12b checks conformance only | — | — |
| `landing/legal.html` | page | 12b | todo |  |  |
| `landing/privacy.html (+ landing/consent.js)` | page | 12b | todo |  |  |
| `landing/404.html (new)` | page | 12b | todo |  |  |
| `landing/og.svg` | page | 12b | todo |  |  |
| `docs/site home (index.md)` | page | 12c | todo |  |  |
| `docs/site pages (theme/hud.css)` | page | 12c | todo |  |  |
| `docs/site local search` | page | 12c | todo |  |  |
| `docs/site 404` | page | 12c | todo |  |  |
| `battlelab-cloud relay/admin_page.py` | page | 12d | todo |  |  |
| `battlelab-cloud catalog gallery.html` | page | 12d | todo |  |  |
| `battlelab-cloud catalog admin.html` | page | 12d | todo |  |  |
| `battlelab-cloud catalog edge 403` | page | 12d | todo |  |  |
| `web/public/og-image.svg + og-image.png` | page | 13 | todo |  |  |
| `web/public/onboarding/* (tour slides)` | page | 13 | todo |  |  |

**Note: `components/files/filePanel.module.css` (Phase 7, partial, #1568).** The row stays `todo`
because the sheet is shared with #950 (the git tab and the viewer/editor are out of this slice). PR 3
converted the shell, tree and upload selectors only: `.gutter` / `.grip`, `.head`, `.headName`,
`.tab` / `.tabBadge`, `.crumbPath` / `.crumbSep` / `.crumbBtn`, `.rowSelected`, `.rowIconDir`,
`.dropRow` / `.dropRoot`, `.queueClose`, `.queueRefusal`, `.queueRow`, plus the new tree- and
shell-scoped classes `.shellBtn` (composes `actionButton` `icon`), `.treeRow`, `.fileState*`,
`.fileRetry` (composes `ghost`), `.fileSkeleton` (composes `nj-skeleton`) and `.queueBar`. None of
these is read by GitTab, BranchMenu, RowMenu or FileViewerModal. Motion: `nj-arrive` on tree rows,
`nj-skeleton`, `nj-acquire` on a sending upload, `nj-glitch-step` on the SendPath flash (all Full
only). Captures: `popup:files`, `popup:upload-menu`, `popup:upload-collision`, `files:tree-open`,
`files:tree-loading`, `files:tree-unavailable`, `files:tree-truncated`, `files:panel-unavailable`,
`files:upload-queue`, `files:send-flash`.
