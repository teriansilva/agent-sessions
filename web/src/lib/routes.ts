/** Every top-level route path, in ONE place (#1058).
 *
 *  `missionLink.ts` already stated the rule and the reason: the `/pulse` → `/mission` rename touched
 *  41 files because the path was a literal at every link site, every route declaration and every
 *  e2e selector. #1058 promotes `/overview` and `/templates` out of the top bar's icon cluster into
 *  the section nav and adds `/ask`, so the same paths are now named in more places again — the
 *  moment to stop spelling them.
 *
 *  `MISSION_PATH` lives here too and `missionLink.ts` re-exports it, so nothing that already
 *  imports it has to change: one home for the values, both import sites valid.
 */

/** The Sessions section's front door — the new-session landing. A session itself is
 *  `/s/:engine/:id`, and the nav links to whichever of the two was last on screen. */
export const SESSIONS_PATH = "/";

/** The Missions section (#948). */
export const MISSION_PATH = "/mission";

/** The pre-#948 mission path, still served as a permanent replace-redirect. */
export const LEGACY_MISSION_PATH = "/pulse";

/** The BattleLab dashboard (#1123): what is running, what needs you, quota, and the Ask field
 *  that opens a conversation on `ASK_PATH` (#1171). */
export const DASHBOARD_PATH = "/dashboard";

/** Ask's old conversation page (#878, #1171). Since #1294 Ask is the right-hand sidebar the corner
 *  icon opens; this path survives only as a link that lands on the dashboard with the sidebar open. */
export const ASK_PATH = "/ask";

/** The session map (#208 / #424). Labelled MAP in the nav; the path is unchanged. */
export const MAP_PATH = "/overview";

/** The instruction-template gallery (#905). */
export const TEMPLATES_PATH = "/templates";

/** Library → Playbooks: bundle gallery and detail (#1192). */
export const PLAYBOOKS_PATH = "/library/playbooks";
export function playbookPath(id: string): string {
  return `${PLAYBOOKS_PATH}/${encodeURIComponent(id)}`;
}
export const PLAYBOOK_NEW_PATH = `${PLAYBOOKS_PATH}/create/new`;
export const playbookEditPath = (id: string) => `${playbookPath(id)}/edit`;

/** The mission checklists editor, under Library in the nav (#1294; it was under Missions). It used to be a Settings
 *  tab (Settings → AI → Checklists); `/settings/ai-playbooks` still redirects here. */
export const CHECKLISTS_PATH = `${MISSION_PATH}/checklists`;

/** Library → Automations (#1201, moved from Missions by #1294 — the URL stayed): missions and sessions that run on a schedule, once, or on Run
 *  now. The list lives here; one automation's run history is `automationPath(id)`, its editor
 *  `automationEditPath(id)`, a new one `AUTOMATION_NEW_PATH`. The server links a failure
 *  notification to `automationPath(id)` too (`notifications.AUTOMATION_PATH`). */
export const AUTOMATIONS_PATH = `${MISSION_PATH}/automations`;
export const AUTOMATION_NEW_PATH = `${AUTOMATIONS_PATH}/new`;
export function automationPath(id: string, runId?: string): string {
  const base = `${AUTOMATIONS_PATH}/${encodeURIComponent(id)}`;
  return runId ? `${base}?run=${encodeURIComponent(runId)}` : base;
}
export function automationEditPath(id: string): string {
  return `${AUTOMATIONS_PATH}/${encodeURIComponent(id)}/edit`;
}

/** The New project wizard (#1187). Under no section of its own: the header nav is measured full at
 *  320 px (#1058), so the wizard is reached from New session, the dashboard and Settings → Projects,
 *  and renders without the session sidebar. */
export const NEW_PROJECT_PATH = "/projects/new";

/** Settings is NOT here: `routes/settingsTabs.ts` already owns `SETTINGS_PATH` and `settingsPath()`
 *  alongside the section list they index, and a second constant for the same path is the drift this
 *  module exists to prevent. It is also not a section — it is an action in the corner cluster and an
 *  item in the operator menu — so the nav never links it.
 */
