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
 *  `""` = explicitly no project, anything else = a chosen project id. There is NO folder field
 *  (#1458): the project owns the launch folder, and a draft may never pick one — an old draft that
 *  still carries a `cwdOverride` key has it dropped by `readDraft`, never applied. */
export interface NewSessionDraft {
  engineChoice: string;
  bypassChoice: boolean | null;
  /** The map-return intent (#936): only `MAP_PATH` means anything. */
  returnTo: string | null;
  projectChoice: string | null;
  /** The model chosen for `engine` (#1189); absent ⇒ `default`. Bound to the engine it was chosen
   *  for, so switching engine clears it. */
  modelChoice?: { engine: string; model: string };
  /** The reasoning level chosen for that engine AND model (#1407); absent ⇒ the preselection. */
  effortChoice?: { engine: string; model: string; effort: string };
  /** An API client the form had GUARDED (#1505): kept through the wizard and the browser's Back
   *  so a stored skip default never turns it into a skip. Guarded-only: no skip is ever carried. */
  apiGuardedEngine?: string;
  /** The specialist the form had chosen (#1693): its id and the version the form saw. Restored only
   *  if that is still the specialist's current version and it can still start; otherwise the form
   *  says it was dropped. */
  specialistChoice?: { id: string; version: number };
}

const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** A draft read back out of router state, every field checked. Anything malformed → `null`.
 *  Unknown keys — a pre-#1458 `cwdOverride`, say — are ignored: parsing an old entry is harmless,
 *  but nothing outside this interface's fields ever reaches the form. */
export function readDraft(v: unknown): NewSessionDraft | null {
  if (!v || typeof v !== "object") return null;
  const d = v as Record<string, unknown>;
  return {
    engineChoice: str(d.engineChoice) ?? "",
    bypassChoice: typeof d.bypassChoice === "boolean" ? d.bypassChoice : null,
    returnTo: d.returnTo === MAP_PATH ? MAP_PATH : null,
    projectChoice: str(d.projectChoice),
    ...readModelChoice(d.modelChoice),
    ...readEffortChoice(d.effortChoice),
    ...(str(d.apiGuardedEngine) ? { apiGuardedEngine: str(d.apiGuardedEngine) as string } : {}),
    ...readSpecialistChoice(d.specialistChoice),
  };
}

function readSpecialistChoice(v: unknown): Pick<NewSessionDraft, "specialistChoice"> {
  if (!v || typeof v !== "object") return {};
  const m = v as Record<string, unknown>;
  const id = str(m.id);
  const version = m.version;
  return id && typeof version === "number" && Number.isInteger(version) && version > 0
    ? { specialistChoice: { id, version } }
    : {};
}

function readEffortChoice(v: unknown): Pick<NewSessionDraft, "effortChoice"> {
  if (!v || typeof v !== "object") return {};
  const m = v as Record<string, unknown>;
  const engine = str(m.engine);
  const model = str(m.model);
  const effort = str(m.effort);
  return engine && model && effort ? { effortChoice: { engine, model, effort } } : {};
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
  /** An API client to start GUARDED (#1505): "New session" after a guarded create failed. Only
   *  ever the guarded direction — a skip choice is never restored from anywhere. */
  apiGuardedEngine?: string;
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
 *  project choice is dropped — the new project and ITS folder replace it (#1458). */
export function finishState(
  draft: NewSessionDraft | null,
  projectId: string,
): LandingRestoreState {
  return {
    ...(draft?.returnTo ? { returnTo: draft.returnTo } : {}),
    ...(draft
      ? { restoreDraft: { ...draft, projectChoice: null } }
      : {}),
    selectProjectId: projectId,
  };
}

/** New session's side: what to seed its state from, read once on mount. */
export function readLandingRestore(state: unknown): {
  draft: NewSessionDraft | null;
  selectProjectId: string | null;
  apiGuardedEngine?: string;
} {
  const s = (state && typeof state === "object" ? state : {}) as Record<string, unknown>;
  const selectProjectId = str(s.selectProjectId) || null;
  const apiGuardedEngine = str(s.apiGuardedEngine);
  return {
    draft: readDraft(s.restoreDraft),
    selectProjectId,
    ...(apiGuardedEngine ? { apiGuardedEngine } : {}),
  };
}
