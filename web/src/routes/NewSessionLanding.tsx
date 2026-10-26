import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { MAP_PATH, useMapWindows } from "../app/workspaceWindows";
import { ApiError, api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import {
  readLandingRestore,
  type NewSessionDraft,
  type WizardEntryState,
} from "../lib/newProject";
import { NEW_PROJECT_PATH } from "../lib/routes";
import {
  engineInfo,
  engineLabel,
  engineName,
  mintsOwnId,
  offeredModels,
  resolveDefault,
  useEngineRoster,
} from "../app/engineRoster";
import { unavailableDefaultNotice } from "../lib/agentDefaults";
import { levelsOf, resolveSessionDefault } from "../lib/sessionDefaults";
import { describeLaunchDefaults } from "../lib/launchDefaults";
import type { SessionDefault } from "../types/api";
import { agentPath, settingsPath } from "./settingsTabs";
import { owningProjectId } from "../lib/projectTree";
import { shortCwd } from "../lib/format";
import { useProjectPicker } from "../app/useProjectPicker";
import { usePrefetchStructuredModels, useStructuredModels } from "../app/useStructuredModels";
import { listAgeMinutes, structuredModelsStore } from "../lib/structuredModelsStore";
import { AgentTiles, ProjectList } from "../components/newSession/NewSessionControls";
import { handStart } from "../components/structured/startHandoff";
import {
  bindingHolds,
  bindingNote,
  bindingOf,
  restoreBinding,
  specialistOptions,
  type SpecialistBinding,
} from "../lib/specialistLaunch";
import type { SpecialistList } from "../types/specialists";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project and start a new session: a
 *  project owns the launch folder (#448), and the form takes it — there is no folder definition
 *  here (#1458). The start summary names the folder it resolved; a missing one is fixed on the
 *  project (Settings → Projects), never from this form. We mint a client-side id, navigate to
 *  /s/:engine/:id carrying the fresh-launch params (cwd + bypass).
 *
 *  Unless the map sent us (#936): pressing "+ New session" with the workspace up carries
 *  `returnTo: "/overview"` in router state, and then the launch goes back to the MAP as a window
 *  rather than taking over the screen. The form itself stays a page either way — it wants the
 *  room, and a 720×480 window has none to spare. */
/** The structured create whose outcome is unknown (#1311), per tab. */
const PENDING_CREATE = "battlelab.pendingStructuredCreate";
/** `model` (#1313), `bypass` (#1339) and `effort` (#1407) are part of the create's identity: a
 *  retry reuses the id only for the same choices, so a reload can never resend an id with a
 *  different one. */
type PendingCreate = {
  engine: string;
  cwd: string;
  model?: string;
  effort?: string;
  id: string;
  bypass?: boolean;
  /** Started from a specialist (#1693): another one, or none, is another create. */
  specialist?: { id: string; version: number };
};

function sameSpecialist(
  a: { id: string; version: number } | undefined,
  b: { id: string; version: number } | undefined,
): boolean {
  return a?.id === b?.id && a?.version === b?.version;
}

/** "12 min old" — how old a kept model list is (#1503). */
function ageText(minutes: number): string {
  return minutes < 1 ? "less than a minute old" : `${minutes} min old`;
}

function readPendingCreate(): PendingCreate | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(PENDING_CREATE) ?? "null") as PendingCreate | null;
    return v &&
      typeof v.engine === "string" &&
      typeof v.cwd === "string" &&
      typeof v.id === "string" &&
      (v.bypass === undefined || typeof v.bypass === "boolean") &&
      (v.effort === undefined || typeof v.effort === "string") &&
      (v.specialist === undefined ||
        (typeof v.specialist?.id === "string" && typeof v.specialist?.version === "number"))
      ? v
      : null;
  } catch {
    return null;
  }
}

function writePendingCreate(v: PendingCreate | null): void {
  try {
    if (v) sessionStorage.setItem(PENDING_CREATE, JSON.stringify(v));
    else sessionStorage.removeItem(PENDING_CREATE);
  } catch {
    /* storage unavailable: the in-flight guard still prevents a same-page double create */
  }
}

/** A late lookup/action must not offer or clear a different attempt's recovery pointer. */
function isStoredAttempt(attempt: PendingCreate): boolean {
  const current = readPendingCreate();
  return current?.id === attempt.id &&
    current.engine === attempt.engine &&
    current.cwd === attempt.cwd &&
    (current.model ?? "default") === (attempt.model ?? "default") &&
    Boolean(current.bypass) === Boolean(attempt.bypass) &&
    sameSpecialist(current.specialist, attempt.specialist);
}

export function NewSessionLanding() {
  const config = useConfig();
  const navigate = useNavigate();
  const location = useLocation();
  const workspace = useMapWindows();
  // Where the operator pressed the button, recorded in router state by the sidebar. Note it is
  // NOT `mapReady`: this form replaced the map, so by the time it renders the map is unmounted
  // and `mapReady` is already false. The request survives that because it is queued on the
  // provider above the router, and the canvas drains it when it comes back.
  const returnToMap =
    (location.state as { returnTo?: string } | null)?.returnTo === MAP_PATH;
  // Back from the New project wizard (#1187): its router state restores this form as the operator
  // left it, and — on finish only — selects the project just created. Read ONCE, on mount.
  const [restore] = useState(() => readLandingRestore(location.state));
  const [engineChoice, setEngineChoice] = useState(
    restore.draft?.engineChoice ?? "",
  );
  // `null` = untouched: the stored default applies (#1128), and `true` — today's behaviour — until
  // the config has loaded. The operator's choice on this form always wins, for this session.
  const [bypassChoice, setConsoleBypass] = useState<boolean | null>(
    restore.draft?.bypassChoice ?? null,
  );
  // An API client's skip choice is SEPARATE and BOUND to the engine it was made for (#1339,
  // Hermes on #1341): a console choice never carries into an API client, switching agents drops
  // it (back to the stored default, #1473), and it is never restored from a draft. It is never
  // replayed — not on reload, not for a retry (Hermes on #1341, rounds 1–2): an unresolved create
  // is resolved by ASKING THE SERVER below, never by re-sending a skip.
  // The one exception is GUARDED (#1505): "New session" after a guarded create failed asks for
  // that client guarded again, whatever the stored default — never a skip it did not choose.
  const [apiBypassChoice, setApiBypassChoice] = useState<{ engine: string; value: boolean } | null>(
    () => {
      const guarded = restore.apiGuardedEngine ?? restore.draft?.apiGuardedEngine;
      return guarded ? { engine: guarded, value: false } : null;
    },
  );
  // An earlier create that the server DID make, found on load (its operation id is its session id).
  const [recovered, setRecovered] = useState<PendingCreate | null>(null);
  useEffect(() => {
    const p = readPendingCreate();
    if (!p) return;
    if (p.bypass) {
      writePendingCreate(null); // a slot from an older build: skip creates keep none (#1339)
      return;
    }
    let live = true;
    // A lookup proves only that the record exists, not that its worker started. Offer it only
    // while its pointer is current; opening or explicitly dismissing it discharges that pointer.
    // A lookup callback itself never clears storage (Hermes 5920, #1373).
    api
      .structuredSnapshot(`${p.engine}:${p.id}`)
      .then(() => {
        if (live && isStoredAttempt(p)) setRecovered(p);
      })
      .catch(() => {
        // Never made (or not yet): the guarded attempt keeps its id for a harmless same-form retry.
      });
    return () => {
      live = false;
    };
  }, []);

  // Project entities own the default launch folder (#448). projectChoice === null = untouched
  // (use the default selection); "" = no project; else an entity id. The list paints from the
  // picker's cache and revalidates on every mount (#1436); `projectsFresh` = that revalidation
  // has settled, and nothing starts before it does (see `canStart`).
  const picker = useProjectPicker();
  const entities = picker.projects ?? [];
  const projectsFresh = picker.fresh;
  // A created project wins; otherwise the draft's own choice comes back in all three states.
  const [projectChoice, setProjectChoice] = useState<string | null>(
    restore.selectProjectId ?? restore.draft?.projectChoice ?? null,
  );
  // An explicit choice the CONFIRMED list no longer has (archived or deleted since it was cached,
  // or since the draft was saved) is void: the selection falls back to the default resolution and
  // the folder follows it — before Start, never after (#1436). Until the list is confirmed, or if
  // confirming it failed, the choice stands (a just-created project is not in the old cache yet,
  // and an unreachable server proves nothing; the server still refuses an unknown id on stamp).
  const choiceVanished =
    !!projectChoice &&
    projectsFresh &&
    !picker.failed &&
    !entities.some((p) => p.id === projectChoice);
  // The operator's starred project (#615 Phase 2). `entities` is already archived-filtered, so an
  // id naming an archived — or since-deleted — project simply isn't found, and we fall back to the
  // first project rather than pre-selecting nothing. Before #615 there was no pref at all: the
  // preselection was `entities[0]`, i.e. whichever project sorted first by name.
  const starredId = config?.default_project_id ?? "";
  const starredExists = entities.some((p) => p.id === starredId);
  const defaultProjectId = (starredExists ? starredId : entities[0]?.id) ?? "";
  const projectSel = (choiceVanished ? null : projectChoice) ?? defaultProjectId;
  const selectedProject = entities.find((p) => p.id === projectSel);
  // The selected project's session defaults (#1508) PRESELECT — between the operator's own choice
  // on this form (which always wins) and the global agent defaults. Never sent as such: the create
  // carries what the form shows. Switching project re-reads them for every field not chosen here.
  const projectDefaults = selectedProject?.launch_defaults ?? {};

  const roster = useEngineRoster();
  const engines = config?.new_session_engines ?? [];
  // The stored default agent, resolved through THE resolver (#1128): used when it can start a new
  // session here, else the first engine that can — and the notice below says so.
  const defaultChoice = resolveDefault(
    roster.engines.filter((e) => engines.includes(e.id)),
    config?.agent_defaults?.default_engine,
    "new",
  );
  // The project's agent, when it can start a session here; otherwise the global default applies
  // (and its own unavailable-default notice), never a guess.
  const projectEngine =
    projectDefaults.engine &&
    resolveDefault(
      roster.engines.filter((e) => engines.includes(e.id)),
      projectDefaults.engine,
      "new",
    ).engine === projectDefaults.engine
      ? projectDefaults.engine
      : null;
  const engine = engineChoice || projectEngine || defaultChoice.engine || engines[0] || "";
  const runtimeOf = (id: string) => engineInfo(id)?.runtime;
  const apiEngines = engines.filter((id) => runtimeOf(id) === "api" || runtimeOf(id) === "chat");
  const consoleEngines = engines.filter((id) => !apiEngines.includes(id));
  const unavailable = config?.unavailable_clients ?? [];
  const isApi = runtimeOf(engine) === "api";
  const apiSource = engineInfo(engine)?.api?.source ?? "";
  const apiCanBypass = isApi && engineInfo(engine)?.api?.can_bypass === true;
  // An API client follows the SAME stored default as a console agent (#1473, operator
  // decision superseding #1339's "unticked by default"): the form's own choice, bound to the engine
  // it was made for, wins for this one create. Only a client whose adapter maps it can skip at all.
  // FAIL CLOSED (adopted from #1457): until a loaded config SAYS the default is on (`=== true` —
  // the server always sends the stored boolean), an API client starts guarded. A missing or
  // late config can never turn a Start already on screen into a skip.
  //
  // A project's permission default (#1508) sits between the two. Its `false` always applies (it is
  // the narrower value). Its `true` applies only from a CONFIRMED project list and a loaded config —
  // a cached list may name a project whose default has since changed, and a form on screen before
  // either has settled must never turn into a skip. `fresh` alone is not confirmation: a FAILED
  // revalidation settles it too and keeps the cached list, which then lets Start go ahead (Hermes
  // on #1532) — so a failed read is no confirmation either.
  const projectBypass =
    projectDefaults.bypass === false ||
    (projectDefaults.bypass === true && projectsFresh && !picker.failed && !!config)
      ? projectDefaults.bypass
      : undefined;
  const bypass = isApi
    ? apiCanBypass &&
      (apiBypassChoice?.engine === engine
        ? apiBypassChoice.value
        : projectBypass !== undefined
          ? projectBypass
          : config?.agent_defaults?.bypass === true)
    : (bypassChoice ?? projectBypass ?? config?.agent_defaults?.bypass ?? true);
  const bypassFromProject =
    projectBypass !== undefined &&
    (isApi ? apiBypassChoice?.engine !== engine : bypassChoice === null);
  const setBypass = (value: boolean) =>
    isApi ? setApiBypassChoice({ engine, value }) : setConsoleBypass(value);

  // The model (#1189), BOUND to the engine it was chosen for: switching engine — by the select or
  // by the default resolving differently — reads as `default` again, never as a model the other
  // engine happens to share a name with. A choice the server no longer offers is sent as chosen and
  // refused there (4422), never quietly swapped for `default` here.
  const [modelChoice, setModelChoice] = useState<{ engine: string; model: string } | null>(
    restore.draft?.modelChoice ?? null,
  );
  // A native API client's models come from its own CLI (#1313); a console agent's from the
  // roster. Each option is what is sent (`id`) and what is shown. Every API client's list is
  // asked for on arrival and kept above the router (#1503), so choosing one paints its last list
  // at once and revalidates behind it; only a client never answered shows "asking…".
  usePrefetchStructuredModels(engines.filter((id) => runtimeOf(id) === "api"));
  const apiModels = useStructuredModels(isApi ? engine : null);
  const apiList = isApi ? apiModels.list : null;
  // The operator's new-session defaults for this API client (#1407) only PRESELECT: what the form
  // shows is what is sent, and a value the client stopped offering is named, never preselected.
  //
  // A project's model (#1508) belongs to the project's OWN agent: it applies only while that agent
  // is the one on the form, and then replaces the global per-client default as the preselection —
  // through the same resolver, so a model the client stopped offering is named, never preselected.
  const projectModel: SessionDefault | null =
    projectEngine && projectEngine === engine && projectDefaults.model
      ? { model: projectDefaults.model, effort: projectDefaults.effort ?? null }
      : null;
  const savedDefault = isApi
    ? (projectModel ?? config?.agent_defaults?.session_defaults?.[engine])
    : undefined;
  const preset = resolveSessionDefault(savedDefault, apiList);
  // A terminal agent's project model preselects only while the roster still offers it.
  const consolePreset =
    !isApi && projectModel && offeredModels(engine).some((m) => m.id === projectModel.model)
      ? projectModel.model
      : null;
  const model =
    modelChoice?.engine === engine
      ? modelChoice.model
      : ((isApi ? preset.model : consolePreset) ?? "default");
  const modelFromProject =
    !!projectModel && modelChoice?.engine !== engine && model === projectModel.model;
  // The level (#1407) is bound to the engine AND model it was chosen for: a model change reads as
  // that model's preselection (or its own level) again, never a level another model offers.
  const [effortChoice, setEffortChoice] = useState<
    { engine: string; model: string; effort: string } | null
  >(restore.draft?.effortChoice ?? null);
  const levels = isApi ? levelsOf(apiList, model) : [];
  const presetEffort = model === preset.model ? preset.effort : null;
  const effort =
    effortChoice?.engine === engine && effortChoice.model === model
      ? effortChoice.effort
      : (presetEffort ?? "default");
  const effortFromProject =
    modelFromProject &&
    !(effortChoice?.engine === engine && effortChoice.model === model) &&
    !!projectModel?.effort &&
    effort === projectModel.effort;
  const defaultsFrom = projectModel
    ? `the ${selectedProject?.name ?? "project"} project’s defaults`
    : `your ${engineLabel(engine)} defaults`;
  const changedFromDefaults =
    preset.model !== null &&
    (model !== preset.model || effort !== (presetEffort ?? "default"));
  const models: { id: string; text: string }[] = isApi
    ? (apiList?.models ?? []).map((m) => ({
        id: m.id,
        text: m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id,
      }))
    : offeredModels(engine).map((m) => ({
        id: m.id,
        text: m.aliases.length ? `${m.id} (${m.aliases.join(", ")})` : m.id,
      }));
  const modelSelect = engineInfo(engine)?.model_select;

  // Start from a specialist (#1693 S4b): choosing one preselects its agent, model and effort as
  // this form's own choices (so they outrank the project's and the global defaults). The binding
  // holds only while the form still shows exactly those: any change and the launch is a plain
  // session — each agent, model or effort change drops the choice, and a binding the form no
  // longer matches is never sent (the server would refuse it).
  const [specialists, setSpecialists] = useState<SpecialistList | null>(null);
  const [poolFailed, setPoolFailed] = useState(false);
  useEffect(() => {
    let live = true;
    // Any failure — a refused read, or a client that throws before it even asks — is "no pool":
    // the form is a plain New session, as before. It never takes the form down with it.
    Promise.resolve()
      .then(() => api.specialists())
      .then((l) => {
        if (!live) return;
        // Only a pool that is one: anything else (an older server, a stub) is "no pool".
        if (Array.isArray(l?.specialists)) setSpecialists(l);
        else setPoolFailed(true);
      })
      .catch(() => {
        if (live) setPoolFailed(true);
      });
    return () => {
      live = false;
    };
  }, []);
  const [specialistChoice, setSpecialistChoice] = useState<SpecialistBinding | null>(null);
  // A choice carried through the New project detour: honoured only as it was (the same version,
  // still startable) once the pool has answered; until then Start waits for it.
  const [restoredSpecialist, setRestoredSpecialist] = useState(
    restore.draft?.specialistChoice ?? null,
  );
  // Only an agent whose launch carries the binding can host one: a chat conversation can't.
  const specialistChoices = specialistOptions(
    specialists,
    engines,
    engines.filter((id) => runtimeOf(id) !== "chat"),
  );
  const restoredBinding =
    restoredSpecialist && specialists ? restoreBinding(specialistChoices, restoredSpecialist) : null;
  const awaitingRestoredSpecialist = !!restoredSpecialist && !specialists && !poolFailed;
  const restoredSpecialistLost =
    !!restoredSpecialist && (poolFailed || (!!specialists && !restoredBinding));
  const chosenSpecialist = specialistChoice ?? restoredBinding;
  const specialist =
    chosenSpecialist && bindingHolds(chosenSpecialist, { engine, model, effort, isApi })
      ? chosenSpecialist
      : null;
  const dropSpecialist = () => {
    setSpecialistChoice(null);
    setRestoredSpecialist(null);
  };
  const chooseSpecialist = (id: string) => {
    const option = specialistChoices.find((o) => o.id === id);
    setRestoredSpecialist(null);
    if (!option || option.blocked) {
      setSpecialistChoice(null);
      return;
    }
    const b = bindingOf(option.spec);
    setEngineChoice(b.engine);
    setModelChoice({ engine: b.engine, model: b.model });
    setEffortChoice({ engine: b.engine, model: b.model, effort: b.effort ?? "default" });
    setApiBypassChoice(null);
    setSpecialistChoice(b);
  };


  // The restore is one-shot: drop it from the history entry (keeping the map return), so a later
  // Back/Forward onto this entry does not re-select the project or re-apply the draft.
  useEffect(() => {
    if (!restore.draft && !restore.selectProjectId) return;
    navigate(location.pathname, {
      replace: true,
      state: returnToMap ? { returnTo: MAP_PATH } : null,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, []);
  // Folder: the project's default, else the legacy global default — never a per-session choice
  // (#1458). `||`, not `??`, between the project's folder and the legacy pref: a folderless project
  // stores "" (#448 back-compat), and "" is a *missing* folder, not a chosen one — fall through to
  // the legacy cwd (#615 Phase 2 edge case). A stale draft's old `cwdOverride` is not even read
  // (`readDraft` drops it): invisible state never picks the launch directory.
  const cwd = selectedProject?.default_folder || config?.default_project || "";
  // The "using your global default" note (#1436) lives in the start summary (#1458): the folder
  // the project does not own needs saying, and that line is where the launch is read.
  const usingGlobalDefault = !!cwd && !selectedProject?.default_folder;

  // Creating a project is the New project wizard's job (#1187). It carries this form's draft so
  // finishing — or cancelling — brings the form back exactly as it is now.
  const draft: NewSessionDraft = {
    engineChoice,
    bypassChoice,
    returnTo: returnToMap ? MAP_PATH : null,
    projectChoice,
    ...(modelChoice ? { modelChoice } : {}),
    ...(effortChoice ? { effortChoice } : {}),
    // A GUARDED API choice survives the detour (#1505); a skip choice never does.
    ...(apiBypassChoice?.value === false ? { apiGuardedEngine: apiBypassChoice.engine } : {}),
    // The chosen specialist survives the detour, re-checked on the way back (#1693).
    ...(specialist ? { specialistChoice: { id: specialist.id, version: specialist.version } } : {}),
  };
  const wizardState: WizardEntryState = { from: "new-session", draft };
  // …and the browser's Back too: before pushing the wizard, the draft is written into THIS entry's
  // state, so returning to it (Back, a phone's back gesture) restores the form as well. The replace
  // is awaited — under the data router `navigate` resolves once it commits — because a push issued
  // in the same tick would interrupt it.
  const openWizard = async (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    await navigate(location.pathname, {
      replace: true,
      state: {
        ...(returnToMap ? { returnTo: MAP_PATH } : {}),
        restoreDraft: draft,
      },
    });
    await navigate(NEW_PROJECT_PATH, { state: wizardState });
  };

  // Nothing launches on a guessed capability (#853 P4): until the roster says whether this engine
  // pins or adopts its id, Start waits. `roster` is read so this re-evaluates when it lands.
  const mintKnown = roster.loaded && mintsOwnId(engine) !== undefined;
  // A saved API default is shown while the client's list is still answering, but nothing starts
  // on it until that list has vouched for it (#1407 PR review): a quick Start must never send the
  // value the screen would then have called stale. Only with NO list for the client (#1503): a
  // cached one vouches, and its background refresh never holds Start.
  const awaitingDefaults = isApi && !!savedDefault && preset.pending && modelChoice?.engine !== engine;
  const checkingSaved = awaitingDefaults && apiList === null && model !== "default";
  const refreshing = isApi && apiList?.status === "ok" && !!apiList.stale && !apiList.refresh_error;
  const refreshFailed =
    isApi && apiList?.status === "ok" && !!(apiList.refresh_error || apiModels.error);
  // A project selection is launched only from a CONFIRMED list (#1436): until this mount's
  // revalidation settles, Start waits — a few ms with the count-free read. "No project", chosen
  // explicitly, has nothing to confirm.
  //
  // Keyed on the operator's CHOICE, not on `projectSel`: before the first list lands `projectSel` is
  // "" only because nothing is known yet, and launching then would skip the starred project that
  // is about to appear (Hermes on #1437, finding 1). Only an explicit "No project" skips the wait.
  const awaitingProjects = !projectsFresh && projectChoice !== "";
  // An explicit choice the list cannot vouch for — not in it, and the refresh failed — has no known
  // folder, so it never launches: not into the global default, not stamped onto an unrelated
  // folder (Hermes on #1437, finding 2). A project the wizard just created is not this case: its
  // create put it in the picker's cache (`api.createProject`).
  const unconfirmedChoice =
    !!projectChoice && picker.failed && !entities.some((p) => p.id === projectChoice);
  // A skip-capable API agent waits for the config (Hermes on #1473): its skip choice starts at the
  // stored default, so until that is known the box would read guarded and then flip — Start must
  // only ever send what the screen has settled on.
  const awaitingConfig = isApi && apiCanBypass && !config;
  const canStart = Boolean(
    engine &&
      cwd &&
      mintKnown &&
      !awaitingDefaults &&
      !awaitingProjects &&
      !awaitingConfig &&
      !unconfirmedChoice &&
      !awaitingRestoredSpecialist,
  );

  const [startError, setStartError] = useState<string | null>(null);
  // Chat creation is single-flight (Hermes on #1219): each `/api/chat/new` mints and persists a
  // conversation, so a second click while one is pending would create a duplicate. The ref is
  // the reservation (set before the await, so a same-tick double click is caught); the state
  // disables the button. `mounted` stops a late completion from navigating after the operator
  // has left the form.
  const chatStarting = useRef(false);
  const [startingChat, setStartingChat] = useState(false);
  const forgetRecovered = () => {
    // Opening may navigate during a retry, but its identity must survive a lost response.
    if (!recovered || chatStarting.current) return;
    if (isStoredAttempt(recovered)) writePendingCreate(null);
    setRecovered(null);
  };
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // A `chat` engine (#1209) launches nothing: its conversation is created on the server, which
  // mints the id, and the session opens like any other.
  const startChat = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    try {
      const { id: key } = await api.chatNew(engine, cwd);
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
        workspace.requestOpen({ key, engine, id, title: "New session" });
        navigate(MAP_PATH);
      } else {
        navigate(`/s/${engine}/${id}`);
      }
      const owningId = owningProjectId(cwd, entities);
      if (projectSel && projectSel !== owningId) {
        api.setSessionProject(key, projectSel).catch(() => {});
      }
    } catch (e) {
      if (!mounted.current) return;
      setStartError(
        e instanceof ApiError && e.message ? e.message : "Couldn’t start that conversation.",
      );
    } finally {
      chatStarting.current = false;
      if (mounted.current) setStartingChat(false);
    }
  };

  // A native API client (#1311) is created on the server too, through the structured route. The
  // operation id is the create's identity: a retry after an unknown outcome reuses it (the server
  // returns the same session), and a different agent or folder is a different create. The
  // unresolved attempt is kept in sessionStorage, so a reload after a lost response retries the
  // SAME create instead of starting a second session (Hermes on #1315).
  const startStructured = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    const prev = readPendingCreate();
    // The model (#1313) and the permission mode (#1339) are part of the create's identity:
    // choosing another one is another create, never a retry that retargets the same operation id.
    const skip = apiCanBypass && bypass;
    const launchSpecialist = specialist
      ? { id: specialist.id, version: specialist.version }
      : undefined;
    // A skip create keeps NO client recovery slot (Hermes 5913): with the two-phase start a lost
    // one can never run, and the server lists it — its view offers Start / Discard until it
    // expires. Only a guarded create reuses an unresolved id.
    const attempt =
      !skip &&
      prev &&
      !prev.bypass &&
      prev.engine === engine &&
      prev.cwd === cwd &&
      (prev.model ?? "default") === model &&
      (prev.effort ?? "default") === effort &&
      sameSpecialist(prev.specialist, launchSpecialist)
        ? prev
        : {
            engine,
            cwd,
            model,
            // Recorded only when chosen, so a create without a level is the one it always was.
            ...(effort !== "default" ? { effort } : {}),
            id: crypto.randomUUID(),
            bypass: skip,
            ...(launchSpecialist ? { specialist: launchSpecialist } : {}),
          };
    if (skip) {
      if (prev?.bypass) writePendingCreate(null);
    } else {
      writePendingCreate(attempt);
    }
    const clearAttempt = () => {
      if (isStoredAttempt(attempt)) writePendingCreate(null);
    };
    try {
      const args = [engine, cwd, attempt.id, model, Boolean(attempt.bypass)] as const;
      const { session_key: key } = await (attempt.specialist
        ? api.structuredCreate(...args, attempt.effort, attempt.specialist)
        : attempt.effort
          ? api.structuredCreate(...args, attempt.effort)
          : api.structuredCreate(...args));
      clearAttempt(); // only THIS attempt's slot — a lost guarded one stays recoverable (5916)
      if (attempt.bypass) {
        // Phase 2 (#1339): only now — the create's response is in hand — does it launch. Fired,
        // not awaited (#1505): the page opens at once, and the request's own outcome goes to the
        // session pane under the same key (a refusal shows its reason; a lost answer says so).
        handStart(key, api.structuredStart(key));
      }
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
        workspace.requestOpen({ key, engine, id, title: "New session" });
        navigate(MAP_PATH);
      } else {
        navigate(`/s/${engine}/${id}`);
      }
      const owningId = owningProjectId(cwd, entities);
      if (projectSel && projectSel !== owningId) {
        api.setSessionProject(key, projectSel).catch(() => {});
      }
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) {
        clearAttempt(); // refused
        // The server's reason is shown below; a refusal may mean the client's list moved on (a
        // CLI upgrade), so the cached list is asked again rather than offered as it was (#1503).
        if (e.status === 422) void structuredModelsStore.revalidate(engine, { force: true });
      }
      if (!mounted.current) return;
      setStartError(
        (e instanceof ApiError && e.message ? e.message : "Couldn’t start that session.") +
          (attempt.bypass && !(e instanceof ApiError && e.status < 500)
            ? " If it was created, it waits in your session list and runs only if you start it there."
            : ""),
      );
    } finally {
      chatStarting.current = false;
      if (mounted.current) {
        setStartingChat(false);
        // A choice on this form covers ONE create (Hermes on #1341, review 5876): after any skip
        // attempt, whatever its outcome, the form falls back to the stored default (#1473).
        if (attempt.bypass) setApiBypassChoice(null);
      }
    }
  };

  const start = () => {
    if (!canStart) return;
    if (engineInfo(engine)?.runtime === "api") {
      void startStructured();
      return;
    }
    if (engineInfo(engine)?.runtime === "chat") {
      void startChat();
      return;
    }
    const id = mintNewSessionId(engine);
    if (!id) return;
    const fresh = {
      cwd,
      bypass,
      ...(model === "default" ? {} : { model }),
      ...(specialist ? { specialist: { id: specialist.id, version: specialist.version } } : {}),
    };
    // Back to the map as a window, when that is where this came from AND the workspace has room
    // under the operator's cap. The request is queued on the workspace and drained by the canvas
    // once it has mounted and measured — the anchor and the overlay box are facts only the map
    // has, so it is the map that opens the window.
    //
    // The `hasRoom` half is not a nicety: at capacity the map would refuse the open and raise a
    // notice, and the launch — with the cwd and bypass choice just made on this form — would be
    // gone (Hermes on #939, finding 2). Starting the session is the operator's actual intent, so
    // a full workspace falls back to the ordinary full-screen launch rather than losing it. Note
    // it does NOT consult `mapReady`: this form replaced the map, so that is already false.
    if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
      workspace.requestOpen(
        { key: `${engine}:${id}`, engine, id, title: "New session" },
        fresh,
      );
      navigate(MAP_PATH);
    } else {
      navigate(`/s/${engine}/${id}`, { state: { fresh } });
    }
    // Stamp the project only when it's an explicit choice folder-resolution wouldn't already
    // produce (#361): a redundant owning entity isn't stamped; "" never stamps.
    const owningId = owningProjectId(cwd, entities);
    if (projectSel && projectSel !== owningId) {
      api.setSessionProject(`${engine}:${id}`, projectSel).catch(() => {});
    }
  };

  // Any explicit project choice settles a vanished one; the folder follows the new project.
  const chooseProject = (id: string) => setProjectChoice(id);

  // Ctrl/⌘+Enter starts — but only with focus INSIDE the form (#1437 review). A document-wide
  // listener fired from the sidebar, the top bar or a dialog beside the form, launching an agent
  // the operator never asked for (and Ctrl+Enter on a link is the browser's background-tab
  // gesture). A held key's auto-repeat is ignored: the console path has no in-flight guard of its
  // own.
  const onFormKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || e.repeat || e.defaultPrevented) return;
    // A focused link keeps its own Ctrl+Enter — the browser's open-in-background-tab.
    if ((e.target as Element).closest?.("a[href]")) return;
    // Nothing starts while a modal is up — whatever has focus: a dialog renders outside this form,
    // and until it moves focus into itself the focus can still sit on a control in here (#1437).
    if (document.querySelector('[aria-modal="true"]')) return;
    e.preventDefault();
    start();
  };

  const permissionNote =
    runtimeOf(engine) === "chat"
      ? ""
      : isApi && !apiCanBypass
        ? "asks before each request"
        : bypass
          ? "skips permission prompts"
          : "asks before each request";

  return (
    <div className={styles.landing}>
      <div className={styles.form} data-new-session-form onKeyDown={onFormKeyDown}>
        <div className={styles.scroll}>
        <div className={styles.card}>
        <div className={styles.brandHero}>
          <div className={styles.wordmark}>
            Battle<b>Lab</b>
          </div>
          <p className={styles.tagline}>Command &amp; Code</p>
        </div>
        <h1>Start a new session</h1>

        {/* Two columns on a wide screen (#1473): what runs on the left, where it runs on
            the right. A narrower pane stacks them; Start stays in the pane's footer. */}
        <div className={styles.columns}>
        <div className={styles.column}>
        {specialistChoices.length > 0 && (
          <section className={styles.section} aria-labelledby="new-session-specialist-head">
            <h2 id="new-session-specialist-head" className={styles.sectionHead}>
              Start from a specialist
            </h2>
            <label className={styles.field}>
              <span>Specialist</span>
              <select
                aria-label="Specialist"
                data-testid="new-session-specialist"
                value={specialist?.id ?? ""}
                onChange={(e) => chooseSpecialist(e.target.value)}
              >
                <option value="">None — a plain session</option>
                {specialistChoices.map((o) => (
                  <option key={o.id} value={o.id} disabled={o.blocked !== null}>
                    {o.text}
                  </option>
                ))}
              </select>
            </label>
            {specialist && (
              <p className={styles.hint} data-testid="new-session-specialist-note">
                {bindingNote(specialist)}
              </p>
            )}
          </section>
        )}
        {restoredSpecialistLost && (
          <p className={styles.hint} role="status" data-testid="new-session-specialist-dropped">
            The specialist you chose can’t start as it was (it changed or is full) — this is a plain
            session unless you choose one again.
          </p>
        )}
        {(engines.length > 1 || unavailable.length > 0) && (
          <section className={styles.section} aria-labelledby="new-session-agent-head">
            <h2 id="new-session-agent-head" className={styles.sectionHead}>
              Agent
            </h2>
            <AgentTiles
              consoleEngines={consoleEngines}
              apiEngines={apiEngines}
              unavailable={unavailable}
              value={engine}
              defaultEngine={defaultChoice.engine ?? ""}
              projectEngine={projectEngine ?? ""}
              onChange={(id) => {
                setEngineChoice(id);
                dropSpecialist(); // another agent: a plain session
                setModelChoice(null); // a model belongs to the engine it was chosen for
                setApiBypassChoice(null); // a skip choice never survives an agent change (Hermes 5913)
              }}
            />
          </section>
        )}

        {/* A chosen model whose list has since gone (discovery failed, or a restored draft) keeps
            its select, so what will be sent stays visible and `default` stays one click away. */}
        {isApi && apiList && apiList.status !== "ok" && (
          <p className={styles.hint} data-testid="new-session-model-api">
            {`Models unavailable — ${apiList.reason ?? "the agent listed no models"}. Only default can start.`}
          </p>
        )}
        {models.length > 0 || model !== "default" ? (
          <label className={styles.field}>
            <span>
              Model
              {modelFromProject && <ProjectBadge />}
              {/* The server is refreshing the list behind this one (#1503): said, never waited on. */}
              {refreshing && (
                <span className={styles.fieldStatus} data-testid="new-session-model-refreshing">
                  refreshing…
                </span>
              )}
            </span>
            <select
              aria-label="Model"
              data-testid="new-session-model"
              value={model}
              onChange={(e) => {
                dropSpecialist(); // another model: a plain session
                setModelChoice({ engine, model: e.target.value });
              }}
            >
              <option value="default">default</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.text}
                </option>
              ))}
              {/* A restored choice the roster no longer lists stays visible, so what is shown is
                  what will be sent — and refused, rather than silently becoming `default`. */}
              {model !== "default" && !models.some((m) => m.id === model) && (
                <option value={model}>
                  {model} {isApi && apiList === null ? "(checking…)" : "(no longer offered)"}
                </option>
              )}
            </select>
          </label>
        ) : isApi ? (
          apiList === null ? (
            <p className={styles.hint} data-testid="new-session-model-loading">
              Model: asking the agent which models it offers…
            </p>
          ) : null
        ) : modelSelect?.configured_elsewhere && engineInfo(engine)?.runtime !== "chat" ? (
          <p className={styles.hint} data-testid="new-session-model-elsewhere">
            Model: set in {engineName(engine)}’s own configuration.
          </p>
        ) : null}

        {checkingSaved && (
          // A saved default no list has confirmed yet (#1503, state d): Start waits for it — but
          // the select stays, so `default` (which needs no list) is one pick away, and any pick
          // ends the wait (#1503 PR review).
          <p className={styles.hint} data-testid="new-session-model-checking">
            Checking your saved model <code>{model}</code>… or pick one to start now.
          </p>
        )}
        {refreshFailed && (
          <p
            className={`${styles.hint} ${styles.degraded}`}
            role="status"
            data-testid="new-session-model-refresh-failed"
          >
            <span className={styles.led} aria-hidden="true" />
            Couldn’t refresh the list — showing the last one ({ageText(listAgeMinutes(apiModels))}).
          </p>
        )}
        {isApi && (levels.length > 0 || effort !== "default") && (
          <label className={styles.field}>
            <span>
              Reasoning effort
              {effortFromProject && <ProjectBadge />}
            </span>
            <select
              aria-label="Reasoning effort"
              data-testid="new-session-effort"
              value={effort}
              onChange={(e) => {
                dropSpecialist();
                setEffortChoice({ engine, model, effort: e.target.value });
              }}
            >
              <option value="default">model’s default</option>
              {levels.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
              {effort !== "default" && !levels.includes(effort) && (
                <option value={effort}>
                  {effort} {apiList === null ? "(checking…)" : "(no longer offered)"}
                </option>
              )}
            </select>
          </label>
        )}
        {isApi && savedDefault && (
          <p className={`${styles.hint} ${styles.defaultsHint}`} data-testid="new-session-defaults">
            {preset.modelStale
              ? `Your default model ${preset.modelStale} is no longer offered — starting with ${engineLabel(apiSource || engine)}’s default. `
              : preset.unverified
                ? `Your default model couldn’t be checked, so only default can start. `
                : preset.effortStale
                  ? `Your default effort ${preset.effortStale} is no longer offered for ${model}. `
                  : changedFromDefaults
                    ? `Changed for this session — ${projectModel ? "the project’s" : "your"} defaults are ${savedDefault.model}${savedDefault.effort ? ` · ${savedDefault.effort}` : ""}. `
                    : preset.pending
                      ? `From ${defaultsFrom}. `
                      : `From ${defaultsFrom}; change them here for this session only. `}
            {changedFromDefaults ? (
              <button
                type="button"
                className={styles.linkButton}
                onClick={() => {
                  dropSpecialist();
                  setModelChoice(null);
                  setEffortChoice(null);
                }}
              >
                Reset to defaults
              </button>
            ) : (
              <Link to={projectModel ? settingsPath("projects") : agentPath(engine)}>
                Edit defaults
              </Link>
            )}
          </p>
        )}

        {roster.loaded && defaultChoice.unavailableDefault && (
          <p className={styles.hint} role="status">
            {unavailableDefaultNotice(
              defaultChoice.unavailableDefault,
              defaultChoice.engine,
            )}
          </p>
        )}

        </div>
        <div className={styles.column}>
        {/* Project FIRST (#448): it owns the launch folder — each row shows it, and the start
            summary names the one this session resolves to (#1458). */}
        <section className={styles.section} aria-labelledby="new-session-project-head">
          <h2 id="new-session-project-head" className={styles.sectionHead}>
            Project
          </h2>
          <ProjectList
            projects={picker.projects}
            // With no list at all yet, an untouched choice checks NOTHING: "No project" would only
            // be standing in for a default not known yet — and a checked row cannot be clicked to
            // choose it (#1437 review). A cached list already knows the default.
            value={picker.projects === null && !projectsFresh && projectChoice === null ? null : projectSel}
            starredId={starredId}
            loading={!projectsFresh}
            failed={picker.failed}
            newProjectTo={NEW_PROJECT_PATH}
            newProjectState={wizardState}
            onNewProject={(e) => void openWizard(e)}
            onChange={chooseProject}
          />

          {/* What the project's sessions start with (#1508) — the fields it fills are badged. */}
          {selectedProject && Object.keys(projectDefaults).length > 0 && (
            <p className={styles.hint} data-testid="new-session-project-defaults">
              <b>{selectedProject.name}</b> starts as{" "}
              {describeLaunchDefaults(projectDefaults, {
                engine: defaultChoice.engine,
                bypass: config?.agent_defaults?.bypass ?? true,
              })}
              .{" "}
              <Link to={settingsPath("projects")}>Edit project defaults</Link>
            </p>
          )}
        </section>

        {/* A `chat` agent (#1209) has no tools, so there are no permission prompts to skip. An
            `api` client (#1339) offers it only when the server says its adapter maps it
            (`api.can_bypass`); the create route re-checks. */}
        {runtimeOf(engine) !== "chat" && (!isApi || apiCanBypass) && (
        <label className={styles.checkbox}>
          <input
            type="checkbox"
            checked={bypass}
            onChange={(e) => setBypass(e.target.checked)}
          />
          <span>
            Skip permission prompts
            {bypassFromProject && <ProjectBadge />}
          </span>
        </label>
        )}
        {isApi && apiCanBypass && bypass && (
          <p className={styles.hint} data-testid="api-bypass-warning">
            {engineLabel(apiSource)} won’t ask before running commands or editing files, and
            runs without its sandbox. Fixed for this session.
          </p>
        )}
        {isApi && recovered?.engine === engine && isStoredAttempt(recovered) && (
          <div className={styles.recovery} role="status" data-testid="api-recovered-create">
            <p>A previous {engineLabel(recovered.engine)} session is saved. Open it to check its status.</p>
            <div className={styles.recoveryActions}>
              <Link
                to={`/s/${recovered.engine}/${recovered.id}`}
                onClick={forgetRecovered}
              >
                Open previous session
              </Link>
              <button
                type="button"
                disabled={startingChat}
                onClick={forgetRecovered}
              >
                Dismiss reminder
              </button>
            </div>
            <p>Dismissing keeps it in your session list.</p>
          </div>
        )}
        </div>
        </div>

        <p className={styles.hint}>
          …or open an existing session from the list.
        </p>
        {startError && (
          <p className={styles.startError} role="alert" data-testid="start-error">
            {startError}
          </p>
        )}
        </div>
        </div>
        <div className={styles.startBar}>
          <p className={styles.summary} data-testid="new-session-summary" data-cwd={cwd}>
            {checkingSaved ? (
              "Checking your saved model…"
            ) : awaitingProjects ? (
              "Checking projects…"
            ) : awaitingConfig ? (
              "Loading your settings…"
            ) : unconfirmedChoice ? (
              "Couldn’t confirm the project you chose — pick it again once your projects load, or choose another."
            ) : engine && cwd ? (
              <>
                Start <b>{engineLabel(engine)}</b> in <code>{shortCwd(cwd)}</code>
                <br />
                {[
                  selectedProject ? `project ${selectedProject.name}` : "no project",
                  usingGlobalDefault ? "using your global default folder" : "",
                  permissionNote,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </>
            ) : selectedProject ? (
              <>
                “{selectedProject.name}” has no default folder — set one in{" "}
                <Link to={settingsPath("projects")}>Settings → Projects</Link>.
              </>
            ) : (
              "Pick a project to start in."
            )}
          </p>
          <button
            type="button"
            className={`${styles.start} shine`}
            disabled={!canStart || startingChat}
            onClick={start}
            aria-keyshortcuts="Control+Enter Meta+Enter"
          >
            Start session
          </button>
        </div>
      </div>
    </div>
  );
}

/** Marks a field the selected project's session defaults filled (#1508). */
function ProjectBadge() {
  return (
    <span className={styles.projectBadge} data-testid="project-badge">
      project
    </span>
  );
}
