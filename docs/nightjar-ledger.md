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
| `app/App.css` | css | 4 | done | route:dashboard route:new-session route:boot popup:session-row-menu headers:topbar headers:topbar-menu | sector fade-in (Full), boot power-on, ≤800px drawer slide-in + backdrop |
| `components/brand/Brand.module.css` | css | 4 | done | route:settings-about popup:whats-new popup:tour headers:topbar | none: the brand kit's logo (#1590) is a still image |
| `components/call/call.module.css` | css | 4 | done | popup:hq-call | the call panel fades in (Full), none in Reduced/Off; LEDs carry the call state (steady outside Full); no other motion |
| `components/agents/Roster.module.css` | css | 11 | done | route:settings-agents route:settings-agents-defaults | the filter Seg lights its segment; status LEDs steady; the usage Gauge is lit at read, no loop |
| `components/agents/SessionDefaults.module.css` | css | 11 | todo |  |  |
| `components/ask/AskCards.module.css` | css | 4 | done | popup:hq | card deploy-in, LED change flash |
| `components/ask/AskConsole.module.css` | css | 4 | done | popup:hq | answer word fade + caret, match-row arrival, portrait scan (Full, ambient), acquire bar |
| `components/ask/AskHome.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring route:dashboard-recent popup:needs-you-details popup:recent-work | rows arrive (slide + tint flash), needs-you LEDs blink in Full (steady in Reduced/Off), first read Acquiring (scan bar) |
| `components/ask/AskSidebar.module.css` | css | 4 | done | popup:hq | drawer slide-in + scrim (≤800px) |
| `components/automations/automations.module.css` | css | 10 | done | route:automations route:automations-list popup:automation-menu route:automation-runs route:automation-editor popup:consent | segments and weekdays light in the chrome on a short colour fade; the run row lights its edge; no ambient motion (the counters are static readouts) |
| `components/chat/ChatPane.module.css` | css | 6 | todo |  |  |
| `components/chat/EditProposal.module.css` | css | 6 | todo |  |  |
| `components/dashboard/Dashboard.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring route:dashboard-unread route:dashboard-recent | readouts flash on change (filter step), LEDs flash on a state change, new rows arrive (slide + tint flash), first read Acquiring (radar / scan bar), re-read keeps its data under the refresh bar (scan, static in Reduced/Off), Recent work grows its row (grid-rows) |
| `components/EnableLoginDetails.module.css` | css | 11 | done | route:settings-loginoff | the disclosure marker turns (transform, --t-fast); Reduced/Off still |
| `components/files/filePanel.module.css` | css | 7 | todo |  |  |
| `components/files/mediaPreview.module.css` | css | 7 | todo |  |  |
| `components/FolderPickerModal.module.css` | css | 3 | done | popup:folder-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/gallery/gallery.module.css` | css | 10 | done | route:skills route:skills-empty route:gallery-compare route:gallery-browse | gallery cards arrive once (slide + tint flash, Full); a file disclosure turns its chevron; the pressed segment lights in the chrome |
| `components/gallery/viewTabs.module.css` | css | 10 | done | route:playbooks route:skills | the current tab underlines in the chrome on a short colour fade (no sliding marker: the tabs are links) |
| `components/hq/HqContactGallery.module.css` | css | 11 | done | route:settings-hq-gallery | none of its own: the selection is an edge, never motion; portraits are posters off Full |
| `components/hq/hq.module.css` | css | 4 | done | popup:hq | portrait loop (Full only, paused with the ambient pause) |
| `components/hq/PrimaryContact.module.css` | css | 5 | done | route:dashboard route:dashboard-acquiring | plate edge lights on hover (no movement); the portrait's own loop is HqPortrait's |
| `components/hud/buttonGlitch.css` | css | 2 | done | gallery:buttons | glitch: filter only |
| `components/HudDialog.module.css` | css | 3 | done | popup:hand-off popup:move-to-project popup:session-brief | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/hud/reactFlow.css` | css | 9 | done | map:expanded canvas:memory-graph canvas:playbook-flow | none: the `--xy-*` tokens, 44 px Controls, panel margins, square handles; each canvas's motion lives with it |
| `components/hud/nightjar.css` | css | 2 | done | gallery:all | atmosphere, power-on, rail pulse, LED breathe/blink/flash, pips, gauge pulse, readout flash, seg/tab marker, disclosure, loaders, arrival, stream |
| `components/links/HandoffStub.module.css` | css | 4 | done | popup:handoff-stub | none (a static notice) |
| `components/links/OpenLinkDialog.module.css` | css | 3 | done | popup:open-link | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/memory/agentAccess.module.css` | css | 10 | done | route:memory route:memory-off | the "for agents without the tool" disclosure fades open |
| `components/memory/importCard.module.css` | css | 10 | done | route:memory route:memory-quarantine | the Acquiring bar on the first read and while an import runs |
| `components/memory/memoryGraph.module.css` | css | 10 | done | route:memory-graph | none of its own (the shared React Flow theme, #1573); the selected note's edge lights |
| `components/newSession/NewSessionControls.module.css` | css | 6 | done | route:new-session newsession:loading newsession:list-failed newsession:no-folder newsession:no-project newsession:many newsession:api | route sector fade (App); tile lock-on (one-shot `nj-glitch-step` as a tile is chosen, Full only); Start press glitch (`.shine`, Full only); `nj-skeleton` scan band on the loading rows (Full only); edge and fill transitions at `--t-fast`; nothing moves in Off |
| `components/overview/overview.css` | css | 9 | done | route:map map:expanded map:folders-live map:chip-menu map:loading map:error map:empty map:empty-projects map:partial map:new-project-error map:group-error map:windows-refused map:toolbar-error map:empty-windows | radar sweep + crosshair drift (transform), live-edge data packets (transform/opacity), working-LED ping (decorative scale), minimap breathe (opacity) — Full only, compositor-only, paused with the page; chip hover tint transition |
| `components/overview/sessionWindow.module.css` | css | 9 | done | map:windows map:window-files map:window-cap map:window-readonly | chrome-button hover tint transition (the window's deploy-in and flash live in the layer) |
| `components/overview/windowLayer.module.css` | css | 9 | done | map:windows map:window-cap map:empty-windows | window deploy-in (fade only, on open and on restore), tray chip arrival, duplicate-open edge flash (opacity on a pseudo-element); the focused tether is lit, never animated |
| `components/playbooks/playbookCanvas.module.css` | css | 10 | done | canvas:playbook-flow route:playbook-editor route:playbook-step | none: a playbook edge carries no live state, so nothing flows (#1573: motion only where a state is live); the rework path is static in --status-degraded |
| `components/playbooks/playbookEditor.module.css` | css | 10 | done | route:playbook-editor route:playbook-step | fields light their edge on hover/focus (colour only); no ambient motion |
| `components/playbooks/playbookPrompts.module.css` | css | 10 | done | route:playbook-step | fields light their edge on hover/focus (colour only); no ambient motion |
| `components/playbooks/playbooks.module.css` | css | 10 | done | route:playbooks route:playbook-detail popup:playbook-delete canvas:playbook-flow | dossier cards arrive once (slide + tint flash, Full; at rest in Reduced/Off) |
| `components/playbooks/ProjectPlaybook.module.css` | css | 10 | todo |  |  |
| `components/plugins/PluginSetup.module.css` | css | 11 | done | route:agent-add | none of its own (the wizard rail is Phase 4's WizardShell) |
| `components/ProjectColorPicker.module.css` | css | 10 | todo |  |  |
| `components/projects/ProjectSessionDefaults.module.css` | css | 10 | todo |  |  |
| `components/pulse/direction.module.css` | css | 8 | done | mission:decisions mission:objective-evidence popup:objective-direction | the AI-drafted card deploys in (fade + 6 px rise; 120 ms fade in Reduced); fields and fact chips: edge transition on hover / focus |
| `components/pulse/mission.module.css` | css | 8 | done | route:mission route:mission-needs-you route:mission-rail-loading mission:console popup:mission-template-picker route:mission-start-refused mission:objective-drag mission:objective-refused route:mission-rail-filtered-empty route:mission-rail-archived-empty route:mission-rail-more route:mission-needs-filtered route:mission-needs-loading route:mission-needs-error mission:objectives-unreadable mission:objectives-pending mission:objectives-unavailable mission:objectives-empty mission:objective-clamped mission:objective-unclamped mission:objective-rename mission:timeline-empty mission:pending | rail rows arrive and their LEDs flash on a change; thread events arrive; the state chip flashes on a transition; Details, objective evidence and Show brief expand; planning pips + Acquiring (see the note) |
| `components/pulse/missionThread.module.css` | css | 8 | done | mission:thread mission:failed mission:console mission:pending | a thread event arriving while the console is open slides in and flashes the HUD tint once (≤3 at once, never on first paint; a fade in Reduced); Show brief / Show text fade in; chevrons turn |
| `components/pulse/NotificationBell.module.css` | css | 4 | done | popup:bell headers:topbar | deploy-in, ≤800px drawer slide-in + scrim, row arrival flash, badge bump (filter) |
| `components/pulse/Orchestrator.module.css` | css | 8 | done | mission:decisions | a decision card deploys in (fade + 6 px rise; 120 ms fade in Reduced); option rows tint their edge on hover |
| `components/sessions/AdoptToMissionModal.module.css` | css | 3 | done | popup:adopt | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/settings/AgentEndpointCard.module.css` | css | 11 | todo |  |  |
| `components/settings/ResourceLimits.module.css` | css | 11 | done | route:settings-resources | none: fields light their edge on focus |
| `components/shares/Shares.module.css` | css | 11 | done | route:settings-sharing route:settings-sharing-revoke-all | none: a live link's LED is steady |
| `components/shell/HelpMenu.module.css` | css | 4 | done | popup:operator-menu | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/shell/OperatorMenu.module.css` | css | 4 | done | popup:operator-menu headers:topbar | tile hover transition |
| `components/shell/RelayChip.module.css` | css | 4 | done | headers:topbar popup:operator-menu | LED breathe (linked) / blink (ends soon), steady in Reduced; the panel is the operator menu's (appear, ≤800px sheet). Relayed-only, so the sweep shows its plate (the HQ/bell plate) and panel chrome; its own states are captured by `e2e/homefree-relay-plate.spec.ts` (#1658) |
| `components/shell/SectionNav.module.css` | css | 4 | done | route:dashboard route:mission headers:topbar headers:topbar-menu | tab tint transition |
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
| `components/templates/SuggestionsPanel.module.css` | css | 10 | done | route:templates-suggestions | the cursor square blinks by opacity (steady in Reduced and Off) over the Acquiring bar; suggestion cards arrive once |
| `components/templates/TemplatePickerModal.module.css` | css | 3 | done | popup:template-picker | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/Compose.module.css` | css | 6 | done | route:session-compose route:session-pane popup:keybar | Talk recording: mic glyph opacity step (Full only), key menu appear / ≤800px sheet-up, Send glitch (.shine) |
| `components/terminal/HandoffModal.module.css` | css | 3 | done | popup:hand-off | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/RuntimeGate.module.css` | css | 6 | done | route:session-opening route:session-opening-failed route:session-not-found | opening pips step in opacity (Full; steady in Reduced and Off); the failed LED's halo is the theme's `--glow-led` |
| `components/terminal/SentMessagesModal.module.css` | css | 3 | done | popup:sent-messages | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/SessionRecapModal.module.css` | css | 3 | done | popup:session-brief | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/terminal/TermUplinkVeil.module.css` | css | 6 | done | route:uplink-veil route:uplink-veil-api | veil fade in/out (--t-slow), log deploy (fade + 6 px rise), acquire bar; Reduced: 120 ms fade in and out via `.nj-fade-ok` + a Reduced exit rule; Off: none (#1570) |
| `components/terminal/Terminal.module.css` | css | 6 | done | route:session-pane popup:head-menu popup:toast headers:session-head | head chip glitch on press (nj-glitch-step, Full only), connect-chip radar (nj-radar), history pill steps (nj-steps), copied toast opacity only — nothing over the terminal moves |
| `components/ui/actionButton.module.css` | css | 2 | done | gallery:buttons | press glitch (filter), hover |
| `components/updates/UpdateProgressPanel.module.css` | css | 11 | done | route:settings-updates | the Acquiring strip scans while the installer runs; the fill widens (--t-slow); Reduced/Off hold a still strip |
| `components/updates/UpdateWhatsNew.module.css` | css | 11 | done | route:settings-updates | none |
| `components/wizard/WizardShell.module.css` | css | 4 | done | route:new-project | progress fill, rail tint |
| `index.css` | css | 1 | done | route:dashboard route:new-session | none: base rules and the token, font, motion and plate imports only |
| `routes/AgentsSettings.module.css` | css | 11 | todo |  |  |
| `routes/Appearance.module.css` | css | 11 | todo |  |  |
| `routes/Ask.module.css` | css | 4 | done | popup:hq | none (layout) |
| `routes/HqCallsSettings.module.css` | css | 11 | done | route:settings-hq-calls | none (layout); the Seg marker and switch knob are the primitives' own |
| `routes/Checklists.module.css` | css | 8 | done | route:checklists route:checklists-list route:checklists-editor popup:checklists-leave | step form fades open (Reduced: 120 ms fade; Off: none), plate and row hover tints |
| `routes/Memory.module.css` | css | 10 | done | route:memory route:memory-graph route:memory-quarantine route:memory-off route:memory-acquiring popup:memory-erase | the View / view-tab marker slides; the memory switch's knob slides; the list and inspector fade in; the first read is Acquiring, then skeleton rows |
| `routes/NewProject.module.css` | css | 10 | todo |  |  |
| `routes/NewSessionLanding.module.css` | css | 6 | done | route:new-session newsession:loading newsession:list-failed newsession:no-folder newsession:no-project newsession:many newsession:api | route sector fade (App); tile lock-on (one-shot `nj-glitch-step` as a tile is chosen, Full only); Start press glitch (`.shine`, Full only); `nj-skeleton` scan band on the loading rows (Full only); edge and fill transitions at `--t-fast`; nothing moves in Off |
| `routes/NightjarGallery.module.css` | css | 2 | done | gallery:all | none (layout) |
| `routes/Onboarding.module.css` | css | 4 | done | popup:tour | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `routes/ProjectsManager.module.css` | css | 11 | todo |  |  |
| `routes/Pulse.module.css` | css | 8 | done | route:mission route:mission-overview-error route:mission-health | none of its own: the route heading and its error line are static |
| `routes/Settings.module.css` | css | 11 | todo |  |  |
| `routes/Templates.module.css` | css | 10 | done | route:templates route:templates-suggestions route:template-editor popup:template-delete | the tabs' marker slides under the open tab and the panel cross-fades; dossier cards arrive once; chips and fields light their edge |
| `routes/Variables.module.css` | css | 10 | done | route:variables route:variables-form | the rail's selection tints in; the Advanced disclosure fades open |
| `homefree/connect.css` | css | 12 | done | connect:signed-out connect:verified connect:dialog connect:connecting connect:error connect:connected | atmosphere (Full, dark), radar + acquire bar while connecting, step LEDs, session LED breathe / warn blink, gate meter (state, every mode) |
| `share/ShareViewer.module.css` | css | 12 | done | share:loading share:password share:password-error share:password-wait share:unavailable share:relink share:nocookie share:offline share:terminal share:open | atmosphere (Full, dark), radar while opening, working bars (Full only), button glitch on press |
| `theme/fonts.css` | css | 1 | done | gallery:all | none (font faces) |
| `theme/motion.css` | css | 1 | done | gallery:all | the mode rules and keyframes themselves |
| `tokens.css` | css | n/a | n/a: the palette itself (literal allowlist) | — | — |
| `whatsnew/WhatsNewDialog.module.css` | css | 4 | done | popup:whats-new | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced; slide image fade |
| `components/ui/menu.module.css` | css | 3 | done | popup:operator-menu popup:session-row-menu popup:dashboard-new | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/ui/toast.module.css` | css | 3 | done | popup:toast | toast in (fade + 6 px rise), out at 120 ms |
| `components/pulse/MissionTitle.module.css` | css | 8 | done | popup:mission-title | the shared dialog chrome's (HudDialog.module.css): deploy-in, scrim fade, ≤800px sheet slide-up |
| `components/shell/FooterTelemetry.module.css` | css | 4 | done | route:dashboard | value bump (filter step, Full only) |
| `components/shell/BootSequence.module.css` | css | 4 | done | route:boot | boot: log lines fade in, acquire bar, layer fades out; panels power on (fade + rise) |
| `app/ShellStates.module.css` | css | 4 | done | route:loading route:error | loader acquire bar (Full), failed panel deploy-in |
| `app/App.tsx` | popup | 4 | done | popup:session-row-menu | ≤800px drawer slide-in + backdrop |
| `components/ask/AskSidebar.tsx` | popup | 4 | done | popup:hq | drawer slide-in + scrim (≤800px) |
| `components/ask/AskSwitcher.tsx` | popup | 4 | done | popup:hq | listbox deploy-in |
| `components/call/CallLayer.tsx` | popup | 4 | done | popup:hq-call | the call panel fades in (Full); 0 ms in Reduced/Off |
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
| `components/newSession/NewSessionControls.tsx` | popup | 6 | done | route:new-session newsession:loading newsession:list-failed newsession:no-folder newsession:no-project newsession:many newsession:api | route sector fade (App); tile lock-on (one-shot `nj-glitch-step` as a tile is chosen, Full only); Start press glitch (`.shine`, Full only); `nj-skeleton` scan band on the loading rows (Full only); edge and fill transitions at `--t-fast`; nothing moves in Off |
| `components/overview/SessionTextDialog.tsx` | popup | 3 | done | popup:session-text | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/playbooks/PlaybookDialog.tsx` | popup | 3 | done | popup:playbook-delete | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/MissionConsole.tsx` | popup | 8 | done | mission:console mission:details-band mission:question | the details band fades in when opened (≤1399px); disclosures expand by grid rows; the header state chip flashes once on a change; the question card deploys in |
| `components/pulse/MissionOverflow.tsx` | popup | 3 | done | popup:mission-overflow popup:mission-confirm-abandon popup:mission-confirm-archive popup:mission-confirm-failed | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
| `components/pulse/MissionRail.tsx` | popup | 8 | done | route:mission-needs-you route:mission-rail-drawer route:mission-rail-loading route:mission-rail-error route:mission-rail-empty route:mission-rail-filtered-empty route:mission-rail-archived-empty route:mission-rail-more | rows arrive (`.rowArrived`, ≤3 at once; Reduced: one fade; Off: none), LEDs steady in the list and flash once on a state change, Acquiring scan bar + skeleton on the first read |
| `components/pulse/MissionTitle.tsx` | popup | 3 | done | popup:mission-title | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/NotificationBell.tsx` | popup | 4 | done | popup:bell | deploy-in, drawer slide-in, row arrival flash |
| `components/pulse/ObjectiveDirectionDialog.tsx` | popup | 3 | done | popup:objective-direction | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sessions/AdoptToMissionModal.tsx` | popup | 3 | done | popup:adopt | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/shares/SharePubliclyModal.tsx` | popup | 3 | done | popup:share-publicly | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sidebar/MoveToProjectModal.tsx` | popup | 3 | done | popup:move-to-project | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/sidebar/RowMenu.tsx` | popup | 3 | done | popup:session-row-menu popup:objective-menu | appear (fade, no rise), ≤800px bottom sheet slide-up + scrim, row tint transition |
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
| `routes/NewSessionLanding.tsx` | popup | 6 | done | route:new-session newsession:loading newsession:list-failed newsession:no-folder newsession:no-project newsession:many newsession:api | route sector fade (App); tile lock-on (one-shot `nj-glitch-step` as a tile is chosen, Full only); Start press glitch (`.shine`, Full only); `nj-skeleton` scan band on the loading rows (Full only); edge and fill transitions at `--t-fast`; nothing moves in Off |
| `routes/Onboarding.tsx` | popup | 4 | done | popup:tour | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `routes/RenameProjectModal.tsx` | popup | 3 | done | popup:rename-project | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `whatsnew/WhatsNewDialog.tsx` | popup | 4 | done | popup:whats-new | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/pulse/MissionHeaderActions.tsx` | popup | 3 | done | popup:unarchive popup:mission-overflow popup:mission-confirm-abandon popup:mission-confirm-archive popup:mission-confirm-failed | deploy-in (fade + 6 px rise + scan), scrim fade, ≤800px sheet slide-up, 120 ms fade in Reduced |
| `components/overview/OverviewCanvas.tsx (React Flow)` | canvas | 9 | done | route:map map:expanded map:folders-live map:chip-menu map:loading map:error map:empty map:empty-projects map:partial map:new-project-error map:group-error map:windows map:toolbar-error map:empty-windows | the shared theme (hud/reactFlow.css), `colorMode` from the theme; tactical grid (fine + major) on `<Background>`; live-edge packets; pan and zoom React Flow's own |
| `components/memory/MemoryGraph.tsx (React Flow)` | canvas | 9 | done | canvas:memory-graph | the shared theme (hud/reactFlow.css), `colorMode` from the theme; no motion of its own (its page, memoryGraph.module.css, is Phase 10) |
| `components/playbooks/PlaybookFlowCanvas.tsx (React Flow)` | canvas | 9 | done | canvas:playbook-flow | the shared theme (hud/reactFlow.css), `colorMode` from the theme; no motion of its own (the editor page is Phase 10) |
| `web/index.html (SPA shell, pre-paint script)` | page | 1 | done | route:boot route:dashboard | none of its own: it pins `data-theme`, `data-motion` and the theme-color (the theme's `--bg-0`) before first paint |
| `src/agent_sessions/templates/login.html` | page | 12 | done | page:login page:login-error page:login-qr | atmosphere (Full, dark; still under prefers-reduced-motion), caret blink, live LED on the QR status |
| `src/agent_sessions/templates/login_totp.html` | page | 12 | done | page:totp page:totp-error | atmosphere (Full, dark), caret blink |
| `src/agent_sessions/templates/change_password.html` | page | 12 | done | page:password page:password-error | atmosphere (Full, dark), caret blink |
| `src/agent_sessions/templates/link_approve.html` | page | 12 | done | page:link-confirm page:link-approved page:link-denied page:link-invalid | atmosphere (Full, dark), attention LED blink on the request |
| `web/share.html → share/ShareViewer.tsx` | page | 12 | done | share:loading share:password share:password-error share:password-wait share:unavailable share:relink share:nocookie share:offline share:terminal share:open | atmosphere (Full, dark), radar while opening, working bars (Full only) |
| `src/agent_sessions/routes/shares.py PLACEHOLDER_PAGE` | page | 12 | done | page:share-placeholder | atmosphere (Full, dark); script-free |
| `web/connect.html (+ homefree/connectState.ts)` | page | 12 | done | connect:signed-out connect:verified connect:dialog connect:connecting connect:error connect:connected | atmosphere (Full, dark; paused hidden/idle), button glitch, gate meter (state, every mode), radar + acquire bar while connecting, step LEDs, session LED breathe / warn blink |
| `landing/index.html` | page | 12b | n/a: rebuilt in NIGHTJAR by #1544 (PR #1548); 12b checks conformance only | — | — |
| `landing/legal.html` | page | 12b | done | site:legal | atmosphere sweep (`lp-sweep`) and the title cursor blink (`lp-blink`); none under prefers-reduced-motion (`landing-pages.spec.ts`) |
| `landing/privacy.html (+ landing/consent.js)` | page | 12b | done | site:privacy site:privacy-on site:privacy-off site:privacy-gpc site:privacy-nojs | atmosphere sweep (`lp-sweep`) and the title cursor blink (`lp-blink`); none under prefers-reduced-motion (`landing-pages.spec.ts`); the consent LED is a state, not a motion |
| `landing/404.html` | page | 12b | done | site:404 | atmosphere sweep (`lp-sweep`) and the title cursor blink (`lp-blink`); none under prefers-reduced-motion (`landing-pages.spec.ts`); the radar is still |
| `landing/og.svg` | page | 12b | done | site:og | none (a still share card) |
| `docs/site home (index.md)` | page | 12c | done | docs:home | the hero's atmosphere sweep band (`docs-sweep`), none under reduced motion (`geometry.spec.ts`) |
| `docs/site pages (theme/hud.css)` | page | 12c | done | docs:page | none (reading pages never move; `geometry.spec.ts`) |
| `docs/site local search` | page | 12c | done | docs:search | the modal deploys once (`docs-deploy`, 180 ms), none under reduced motion |
| `docs/site 404` | page | 12c | done | docs:404 | none (a still radar) |
| `battlelab-cloud relay/admin_page.py` | page | 12d | n/a: restyled in its own repo by battlelab-cloud#81 (PR battlelab-cloud#82), with its captures there | — | — |
| `battlelab-cloud catalog gallery.html` | page | 12d | n/a: restyled in its own repo by battlelab-cloud#81 (PR battlelab-cloud#82), with its captures there | — | — |
| `battlelab-cloud catalog admin.html` | page | 12d | n/a: restyled in its own repo by battlelab-cloud#81 (PR battlelab-cloud#82), with its captures there | — | — |
| `battlelab-cloud catalog edge 403` | page | 12d | n/a: restyled in its own repo by battlelab-cloud#81 (PR battlelab-cloud#82), with its captures there | — | — |
| `web/public/og-image.svg + og-image.png` | page | 13 | n/a: a still generated from the brand kit by `web/scripts/render-brand-assets.mjs`; already the NIGHTJAR card since #1590 (PR #1595), pinned by `brandAssets.test.ts` | — | — |
| `web/public/onboarding/* (tour slides)` | page | 13 | done | popup:tour | none: still images (re-captured from the NIGHTJAR UI with `web/visual/promo/capture.mjs`; the Home Free and Settings slides redrawn) |

**Note: Settings B (#1574, Phase 11 part: agents, system, about).** `routes/AgentsSettings.module.css`
stays `todo`: its remaining literal sizes sit in selectors the fenced agent page (`AgentDetail`,
`AddedModels`, #1311 / #1313) reads (`badge`, `kvRow`, `empty`, `stale`, `models`, …), left
byte-identical. Converted for the Defaults page only: `opt` (the chosen card on the accent edge and
tint), `optLabel`, `warn` (the degraded callout), `tog` / `switch` (the thumb slides by transform
onto the HUD fill), `where`, `label`, `saved`, `headLink`, `badgeSm`, `summary`. In
`components/agents/Roster.module.css` the selectors the fenced page shares (`disclosure`,
`summary`, `checks`, `muted`, `inlineError`, `heroMeta`, `techSummary`) took tokens of the SAME
value only (13 px → `--fs-row`, 12 px → `--fs-meta`), so their computed style is unchanged. In
`routes/Settings.module.css` (Settings A's row) this slice converted only the selectors its sections
alone read: the 2FA enrollment (`enrollPanel` as `nj-frame-sm`, `manualKey`, `codeInput` as the
nj-field look, `twofaBadge` / `twofaOn` / `twofaOff` as a mono tag), About's links (`aboutLink`
on the shared ghost compact plate) and support CTA (`coffee` on the primary compact plate),
`updateActions`, `meta` (dt as mono caps), `blurb`; the About brand line (`brandLine`) is left for
the logo PR (#1590), and `.qr` keeps its literal white ground — a scanner needs it in both themes.
Captures: the manifest's PR4b settings B block (FAKE secrets, `visual/promo/settingsStates.mjs`).

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

**Note: `components/pulse/mission.module.css` (Phase 8, #1573).** The sheet is shared by the rail,
the new-mission page, the console, HQ's AskComposer / AskConsole and the dashboard, so PR 4a converted
it in two halves. **Rail half:** the selectors only MissionRail, MissionLanding and NewMissionForm read —
`.railLed` (+ `.railLedStarting`, `.railLedDone`; rows wear the shared `Led`, so `.dotNeedsYou`,
`.railDot` and `.rail .dot` are deleted), `.railNotice*`, `.railLoading`, `.railSkeleton`, the landing
(`.landing*`; NEEDS YOU as an `nj-frame-sm` section with steady `Led` rows, its error as `nj-error`, its
read as the Acquiring loader) and the form's own `.newMission*`. **Console half:** the frame (head row,
state chip with its one-shot flash), the state dots (7 px; the chip keeps its pinned 6 px), notices and
empty states, the thread as hairline rows (scoped under `.console`, because HQ and the dashboard read
`.pane`, `.event`, `.eventHead`, `.eventText` and the composer dock), the Now strip, the details column
and band (grid-row disclosures), objectives, context, the plan card in every state, the question card,
the supervisor's reading, the timeline and auto-choose; 27 dead classes deleted, plus the dead
`.railList` / `.railWithSessions`. Every mission surface reads ONE state→light mapping
(`missionLed`, missionState.ts). The composer box (`.composerBox`, `.composerFoot`, `.foot*`) is
untouched: it is one composer with HQ's (#967), pinned by `mission-control-polish`; `.composerInput`'s
16 px reads the new `--fs-input` token (a field a phone must not zoom into). HQ and the dashboard were
diffed pixel for pixel against the base build: identical. `missionDrawer.module.css` was dead (#948)
and is deleted. Captures: the manifest's PR4a rail and console blocks (`route:mission*`, `mission:*`,
`popup:mission-template-picker`).

**Note: `routes/Settings.module.css` (Phase 11, partial, #1574 — Settings A).** The row stays `todo`:
the sheet is shared with the Agents, System and About pages (Settings B) and with the fenced Projects
and Maintenance selectors (#1575). Settings A converted every selector the shell, General → Session
defaults, Appearance and AI pages read: the shell (`.wrap`, `.head`, `.back` as the compact icon
plate, `.split`, `.sidebar`, `.page`, `.crumb` in the kicker's voice, the sentence-case `.nav` with the
accent fill, the phone `.index` as framed lists), `.section` as THE panel (`nj-frame`, the callsign
behind the hazard notch; `.section .subhead` the small notched callsign, outranking Maintenance's later
`.subhead`), `.hint` / `.blurb`, the radio cards `.themeCard` and the font cards `.fontCard` as plates,
the fields (`.aiInput`, `.fontCustomInput` as `nj-field`; `.aiSecretReadout`; `.aiKeyBadge` and
`.promptBadge` as stamps), the size stepper (`.termSizeStep` as icon plates), the endpoint strip as a
quiet frame, `.cardFoot > .err` as an alert block, the prompts rows (`.promptRow` quiet frames,
`.promptNote` / `.lockBand` hud-edged callouts, `.promptError` as `nj-error`), the new `.callout` /
`.calloutWarn` (YOLO and the AI-written directions heads-up) and the activity rows; every dead fallback
and px font size in those selectors is a token. Captures: the manifest's PR4b settings A block
(`route:settings-*`).

**Note: `routes/Appearance.module.css` (Phase 11, partial, #1574 — Settings A).** The row stays `todo`:
the API-sessions selectors at its end belong to `AppearanceApi.tsx`, fenced for #1311 / #1313. Settings
A converted the rest: the mode, scheme and new motion cards as plates (the lit rail and `--hud` edge for
the choice), the MiniApp reading the canonical tokens through `[data-theme-scope]` (tokens.css; the
custom accent re-applied for the preview's mode by `accentVars`) — no `--m-*` palette — the Motion
control's stages (Full: a compositor sweep and a breathing light, each folded into one 19.2 s iteration
— `stageSweep6`, `stageBreathe8` — so the idle page wakes rarely; paused with the ambient loops), the swatches, `.hex` as `nj-field`, and the scheme samples (their palettes stay
allowlisted content). The shared `.radio`, `.reset`, `.schemeLabel`, `.schemeName` and `.tag` keep their
look for the fenced page. Captures: `route:settings-interface*`, `route:settings-terminal`.
