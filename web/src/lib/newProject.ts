/** The New project wizard's entry, return and New-session handoff (#1187).
 *
 *  Where the wizard was entered from travels in ROUTER STATE, never the query string, and is
 *  matched against a CLOSED map of keys to route constants. Nothing here ever reads a URL out of
 *  the location, so there is no open redirect: an unknown or missing `from` has no return target
 *  at all, and the DONE step then offers only its own actions.
 *
 *  Entered from New session, the wizard also carries that form's DRAFT (agent, bypass, the map
 *  return, the project/folder selection) so finishing or cancelling can put the form back the way
 *  the operator left it. Every field is re-validated on the way in and out: router state is
 *  whatever the history entry holds, and a reload or a hand-edited entry must not crash the form.
 */
import { MAP_PATH, DASHBOARD_PATH, SESSIONS_PATH } from "./routes";
import { settingsPath } from "../routes/settingsTabs";

/** The closed set of entry points. The operator asked for the dashboard's button (2026-09-29) on
 *  top of the two the issue named. */
export const NEW_PROJECT_RETURN = {
  "new-session": SESSIONS_PATH,
  "settings-projects": settingsPath("projects"),
  dashboard: DASHBOARD_PATH,
} as const;

export type NewProjectFrom = keyof typeof NEW_PROJECT_RETURN;

/** `from` → its route constant, or `null` for anything outside the closed map (including
 *  inherited keys such as `"constructor"` and non-strings). */
export function returnTarget(from: unknown): string | null {
  if (typeof from !== "string") return null;
  if (!Object.prototype.hasOwnProperty.call(NEW_PROJECT_RETURN, from)) return null;
  return NEW_PROJECT_RETURN[from as NewProjectFrom];
}

/** New session's form, as the operator left it. The fields mirror `NewSessionLanding`'s own state,
 *  including `projectChoice`'s three states: `null` = untouched (the default selection applies),
 *  `""` = explicitly no project, anything else = a chosen project id. */
export interface NewSessionDraft {
  engineChoice: string;
  bypassChoice: boolean | null;
  /** The map-return intent (#936): only `MAP_PATH` means anything. */
  returnTo: string | null;
  projectChoice: string | null;
  cwdOverride: string | null;
  /** The model chosen for `engine` (#1189); absent ⇒ `default`. Bound to the engine it was chosen
   *  for, so switching engine clears it. */
  modelChoice?: { engine: string; model: string };
}

const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** A draft read back out of router state, every field checked. Anything malformed → `null`. */
export function readDraft(v: unknown): NewSessionDraft | null {
  if (!v || typeof v !== "object") return null;
  const d = v as Record<string, unknown>;
  return {
    engineChoice: str(d.engineChoice) ?? "",
    bypassChoice: typeof d.bypassChoice === "boolean" ? d.bypassChoice : null,
    returnTo: d.returnTo === MAP_PATH ? MAP_PATH : null,
    projectChoice: str(d.projectChoice),
    cwdOverride: str(d.cwdOverride),
    ...readModelChoice(d.modelChoice),
  };
}

function readModelChoice(v: unknown): Pick<NewSessionDraft, "modelChoice"> {
  if (!v || typeof v !== "object") return {};
  const m = v as Record<string, unknown>;
  const engine = str(m.engine);
  const model = str(m.model);
  return engine && model ? { modelChoice: { engine, model } } : {};
}

/** Router state the entry points hand the wizard. */
export interface WizardEntryState {
  from: NewProjectFrom;
  draft?: NewSessionDraft;
}

/** What the wizard was entered with: the `from` key (only when it is in the closed map) and, for
 *  New session, the draft. */
export function readWizardEntry(state: unknown): {
  from: NewProjectFrom | null;
  draft: NewSessionDraft | null;
} {
  const s = (state && typeof state === "object" ? state : {}) as Record<string, unknown>;
  const from = returnTarget(s.from) !== null ? (s.from as NewProjectFrom) : null;
  return { from, draft: from === "new-session" ? readDraft(s.draft) : null };
}

/** Router state New session reads on arrival from the wizard. `restoreDraft` comes back on both
 *  finish and cancel; `selectProjectId` ONLY on finish, when a project really was created. */
export interface LandingRestoreState {
  returnTo?: string;
  restoreDraft?: NewSessionDraft;
  selectProjectId?: string;
}

/** Cancel (leaving before CREATE): the draft exactly as it was, and never a project id. */
export function cancelState(draft: NewSessionDraft | null): LandingRestoreState {
  if (!draft) return {};
  return {
    ...(draft.returnTo ? { returnTo: draft.returnTo } : {}),
    restoreDraft: draft,
  };
}

/** Finish: the draft's agent/bypass/map-return intent, plus the new project to select. The prior
 *  project/folder choice is dropped — the new project and ITS folder replace it. */
export function finishState(
  draft: NewSessionDraft | null,
  projectId: string,
): LandingRestoreState {
  return {
    ...(draft?.returnTo ? { returnTo: draft.returnTo } : {}),
    ...(draft
      ? { restoreDraft: { ...draft, projectChoice: null, cwdOverride: null } }
      : {}),
    selectProjectId: projectId,
  };
}

/** New session's side: what to seed its state from, read once on mount. */
export function readLandingRestore(state: unknown): {
  draft: NewSessionDraft | null;
  selectProjectId: string | null;
} {
  const s = (state && typeof state === "object" ? state : {}) as Record<string, unknown>;
  const selectProjectId = str(s.selectProjectId) || null;
  return { draft: readDraft(s.restoreDraft), selectProjectId };
}
