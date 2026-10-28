import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
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
  familyMark,
  familyOf,
  mintsOwnId,
  offeredModels,
  pickInFamily,
  resolveDefault,
  useEngineRoster,
} from "../app/engineRoster";
import { reloadRoster } from "../app/reloadRoster";
import { readFamilyChoices, rememberFamilyChoice, rememberedFor } from "../lib/familyChoice";
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
import {
  createStructuredSession,
  isStoredAttempt,
  readPendingCreate,
  writePendingCreate,
  type PendingCreate,
} from "../lib/structuredCreate";
import {
  bindingHolds,
  bindingNote,
  bindingOf,
  restoreBinding,
  specialistOptions,
  type SpecialistBinding,
} from "../lib/specialistLaunch";
import type { SpecialistList } from "../types/specialists";
import type { WindowSeed } from "../components/overview/useWorkspace";
import type { FreshSession } from "../lib/termUrl";
import { openFullScreen, type LaunchOutcome } from "../lib/newSessionLaunch";
import { STALE_DEFAULT_STATE } from "../components/terminal/SessionOpening";
import { setupPath } from "../components/plugins/model";
import button from "../components/ui/actionButton.module.css";
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
/** A create refused because the family's automatic default moved (#1728): the server's 409
 *  carries the new default. */
function isStaleDefault(record: unknown): boolean {
  const d = (record as { default?: unknown } | undefined)?.default;
  return !!d && typeof d === "object" && "reason" in d;
}

/** "12 min old" — how old a kept model list is (#1503). */
function ageText(minutes: number): string {
  return minutes < 1 ? "less than a minute old" : `${minutes} min old`;
}

/** The page host (`/new`, #1648): today's behaviour. Back to the map as a window when the map
 *  sent the operator here (`returnTo`, which the New project wizard's round trip carries) AND the
 *  workspace has room; otherwise full screen. The map's modal is the other host
 *  (`NewSessionModal`). */
export function NewSessionLanding() {
  const navigate = useNavigate();
  const location = useLocation();
  const workspace = useMapWindows();
  // Note it is NOT `mapReady`: this form replaced the map, so the map is unmounted and `mapReady`
  // is already false. The request survives that because it is queued on the provider above the
  // router, and the canvas drains it when it comes back. The `hasRoom` half is not a nicety: at
  // capacity the map would refuse the open, and the launch — with the choices just made on this
  // form — would only come back through the drain's hand-back (Hermes on #939, finding 2).
  const returnToMap = (location.state as { returnTo?: string } | null)?.returnTo === MAP_PATH;
  const onLaunched = (outcome: LaunchOutcome) => {
    if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
      if (outcome.fresh) workspace.requestOpen(outcome.seed, outcome.fresh);
      else workspace.requestOpen(outcome.seed);
      navigate(MAP_PATH);
      return;
    }
    openFullScreen(navigate, outcome);
  };
  return <NewSessionForm host="page" onLaunched={onLaunched} />;
}

/** The New session form (#1648): one form, two hosts — the page (`NewSessionLanding`) and the
 *  map's modal (`NewSessionModal`). The create, its operation id, its single-flight guard and its
 *  recovery slot live HERE and are the same for both. The form never decides where a launched
 *  session opens: it emits the outcome (`onLaunched`) and its host decides. `host` only changes
 *  the presentation (the modal drops the page's display heading, says when the map is full, and
 *  marks the detours that leave the map). `onBusy` reports a create in flight, so the modal
 *  cannot be closed under it. */
export function NewSessionForm({
  host = "page",
  onLaunched,
  onBusy,
}: {
  host?: "page" | "map";
  onLaunched: (outcome: LaunchOutcome) => void;
  onBusy?: (busy: boolean) => void;
}) {
  const config = useConfig();
  const navigate = useNavigate();
  const location = useLocation();
  const workspace = useMapWindows();
  const inModal = host === "map";
  // Where the operator pressed the button, recorded in router state by the sidebar. Note it is
  // NOT `mapReady`: this form replaced the map, so by the time it renders the map is unmounted
  // and `mapReady` is already false. The request survives that because it is queued on the
  // provider above the router, and the canvas drains it when it comes back. In the modal the
  // map is right there, so the map is where it returns (a detour to the wizard carries it).
  const returnToMap =
    inModal || (location.state as { returnTo?: string } | null)?.returnTo === MAP_PATH;
  // In the modal the map is mounted, so `mapReady` is the live answer to "can it host a window",
  // and `hasRoom` to "is there room". Full = a map that could host one but has no room: Start then
  // says it opens full screen, before anything is created (#1648).
  const mapFull = inModal && !!workspace?.mapReady && !workspace.hasRoom;
  // Back from the New project wizard (#1187): its router state restores this form as the operator
  // left it, and — on finish only — selects the project just created. Read ONCE, on mount.
  const [restore] = useState(() => readLandingRestore(location.state));
  // A terminal launch the server refused because its automatic default moved (4412, #1728) comes
  // back here with the server's words: the form explains it, reloads the agents, and Start is the
  // operator's deliberate retry. Read ONCE, on mount.
  const [arrivedStale] = useState(() => {
    const v = (location.state as Record<string, unknown> | null)?.[STALE_DEFAULT_STATE];
    return typeof v === "string" && v ? v.slice(0, 300) : null;
  });
  const refreshConfig = useConfigRefresh();
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
  // API first (#1728): the default agent picks a FAMILY, and the server's family decision picks
  // its client — a ready API client, else the console — unless this device remembers the
  // operator's own pick for that family (`remembered_explicit`; a remembered client that can no
  // longer start is ignored, `remembered_stale`). A tile picked here, a project's own agent or a
  // specialist are explicit choices and are never replaced. An older server sends no families:
  // then nothing here applies and the default is exactly the one before.
  const [remembered, setRemembered] = useState(() => readFamilyChoices());
  // Whether the agent on the form was picked by a tile here (rather than a specialist or a draft):
  // only such a pick is remembered for its family once it starts.
  const [tilePicked, setTilePicked] = useState(false);
  // The roster is loaded once per app; readiness moves (a sign-in, an update), so the form asks
  // again on arrival. The create re-checks anyway, and refuses a default that moved (409 / 4412).
  useEffect(() => {
    void reloadRoster();
  }, []);
  // The CONFIGURED agent's family decides first: a stored default whose own client cannot start
  // (an unready API client) stands in with another client of the SAME family (its console), never
  // with whatever agent happens to come first in roster order. Only when no client of that family
  // can start does the cross-family fallback (`resolveDefault`) choose — and then its family.
  const storedDefault = config?.agent_defaults?.default_engine ?? null;
  const storedFamily = storedDefault ? familyOf(storedDefault) : undefined;
  const pickFor = (fam: NonNullable<ReturnType<typeof familyOf>>) =>
    pickInFamily(fam, engines, rememberedFor(remembered, fam.family));
  const storedPick = storedFamily ? pickFor(storedFamily) : null;
  const fallbackFamily = defaultChoice.engine ? familyOf(defaultChoice.engine) : undefined;
  const globalPick = storedPick?.engine ? storedPick : fallbackFamily ? pickFor(fallbackFamily) : null;
  // The global default as it resolves here: the family's client, else the plain resolver's.
  const globalEngine = globalPick?.engine ?? defaultChoice.engine;
  const familyPick = !engineChoice && !projectEngine && globalPick?.engine ? globalPick : null;
  const engine =
    engineChoice || projectEngine || familyPick?.engine || defaultChoice.engine || engines[0] || "";
  // What the create states about the client it names: the family's automatic default (refused by
  // the server if that default has moved since this screen) or the operator's explicit choice
  // (honoured while it can start). Unsaid when the server gave no families (an older server).
  const createChoice: "automatic" | "explicit" | undefined =
    engineChoice || projectEngine
      ? familyOf(engine)
        ? "explicit"
        : undefined
      : familyPick?.engine === engine
        ? familyPick.choice
        : undefined;
  // Each family's preselected client carries its mark on its tile (#1728 mockup).
  const familyMarks: Record<string, { word: string; sub?: string }> = {};
  for (const fam of roster.families) {
    const pick = pickInFamily(fam, engines, rememberedFor(remembered, fam.family));
    const mark = familyMark(pick);
    if (pick.engine && mark) familyMarks[pick.engine] = mark;
  }
  // A pick made on a tile here is remembered for its family once it starts, so an explicit
  // Terminal pick keeps overriding the API preselection — also when the API client becomes ready
  // LATER (a Terminal pick made while it stood in as the default is still the operator's choice).
  // Only an explicit pick of the family's API default forgets it: the default applies again.
  const noteExplicitPick = () => {
    if (!tilePicked) return;
    const fam = familyOf(engine);
    if (!fam || !fam.clients.some((c) => engineInfo(c)?.runtime === "api")) return;
    const apiDefault =
      engine === fam.interactive_default.engine && engineInfo(engine)?.runtime === "api";
    rememberFamilyChoice(fam.family, apiDefault ? null : engine);
    setRemembered(readFamilyChoices());
  };
  const forgetFamilyPick = (family: string) => {
    rememberFamilyChoice(family, null);
    setRemembered(readFamilyChoices());
  };
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
    setTilePicked(false); // the specialist chose this agent, not the operator's own pick
    setModelChoice({ engine: b.engine, model: b.model });
    setEffortChoice({ engine: b.engine, model: b.model, effort: b.effort ?? "default" });
    setApiBypassChoice(null);
    setSpecialistChoice(b);
  };


  // The restore is one-shot: drop it from the history entry (keeping the map return), so a later
  // Back/Forward onto this entry does not re-select the project or re-apply the draft.
  useEffect(() => {
    if (!restore.draft && !restore.selectProjectId && !arrivedStale) return;
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

  const [startError, setStartError] = useState<string | null>(() =>
    arrivedStale ? `${arrivedStale}. Check the agent above and press Start again.` : null,
  );
  useEffect(() => {
    if (!arrivedStale) return;
    void reloadRoster();
    refreshConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on arrival
  }, []);
  // Chat creation is single-flight (Hermes on #1219): each `/api/chat/new` mints and persists a
  // conversation, so a second click while one is pending would create a duplicate. The ref is
  // the reservation (set before the await, so a same-tick double click is caught); the state
  // disables the button. `mounted` stops a late completion from navigating after the operator
  // has left the form.
  const chatStarting = useRef(false);
  const [startingChat, setStartingChat] = useState(false);
  // A create in flight holds the modal open (#1648): closing it would drop the outcome's window.
  useEffect(() => onBusy?.(startingChat), [startingChat, onBusy]);
  // ...and so does every route-leaving link in this form (Hermes on #1717): a detour (New
  // project, Settings, a previous session) unmounts the form, and the create's answer would then
  // open nothing although the server made the session. Caught on the way DOWN, so neither the
  // link's own handler nor its navigation runs; a keyboard Enter on a link is a click too.
  // The one exception is "Open previous session" (`data-during-create`): it opens the very create
  // this one is retrying, under the same operation id, and its recovery slot outlives the form
  // (#1373), so leaving loses nothing.
  const holdLinksWhileCreating = (e: MouseEvent<HTMLDivElement>) => {
    if (!startingChat) return;
    const link = (e.target as Element).closest?.("a[href]");
    if (!link || link.hasAttribute("data-during-create")) return;
    e.preventDefault();
    e.stopPropagation();
  };
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
      noteExplicitPick();
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      openLaunched({ key, engine, id, title: "New session" });
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

  // A native API client (#1311) is created on the server too, through the structured route — the
  // shared `createStructuredSession` (#1739), which owns the operation id, its recovery slot and the
  // two-phase skip start. The model (#1313), level (#1407), specialist (#1693) and permission mode
  // (#1339) are part of the create's identity: choosing another one is another create.
  const startStructured = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    const skip = apiCanBypass && bypass;
    try {
      const key = await createStructuredSession({
        engine,
        cwd,
        model,
        effort,
        bypass: skip,
        ...(specialist ? { specialist: { id: specialist.id, version: specialist.version } } : {}),
        ...(createChoice ? { choice: createChoice } : {}),
      });
      noteExplicitPick();
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      openLaunched({ key, engine, id, title: "New session" });
      const owningId = owningProjectId(cwd, entities);
      if (projectSel && projectSel !== owningId) {
        api.setSessionProject(key, projectSel).catch(() => {});
      }
    } catch (e) {
      // A refusal has released its slot (the helper); the server's reason is shown below. It may
      // mean the client's list moved on (a CLI upgrade), so the cached list is asked again rather
      // than offered as it was (#1503).
      if (e instanceof ApiError && e.status === 422) {
        void structuredModelsStore.revalidate(engine, { force: true });
      }
      if (!mounted.current) return;
      // The automatic default moved between this screen and Start (#1728): nothing was started.
      // Ask the roster again, so the form shows the new default, and say so — never launch it
      // without the operator seeing it.
      if (e instanceof ApiError && e.status === 409 && isStaleDefault(e.record)) {
        refreshAgents();
        setStartError(`${e.message}. Check the agent above and press Start again.`);
        return;
      }
      setStartError(
        (e instanceof ApiError && e.message ? e.message : "Couldn’t start that session.") +
          (skip && !(e instanceof ApiError && e.status < 500)
            ? " If it was created, it waits in your session list and runs only if you start it there."
            : ""),
      );
    } finally {
      chatStarting.current = false;
      if (mounted.current) {
        setStartingChat(false);
        // A choice on this form covers ONE create (Hermes on #1341, review 5876): after any skip
        // attempt, whatever its outcome, the form falls back to the stored default (#1473).
        if (skip) setApiBypassChoice(null);
      }
    }
  };

  /** The launched session, handed to the host, which decides where it opens (#1648). */
  const openLaunched = (seed: WindowSeed, fresh?: FreshSession) =>
    onLaunched(fresh ? { seed, fresh } : { seed });

  // The agents changed under the form (#1728): ask the server again for the roster AND the startable
  // list, so the form shows the default it would now choose.
  const refreshAgents = () => {
    void reloadRoster();
    refreshConfig();
  };
  const staleWords = (fam: NonNullable<ReturnType<typeof familyOf>>) => {
    const now = fam.interactive_default.engine;
    return now
      ? `The default for ${fam.family} changed to ${engineLabel(now)} since this was shown — nothing was started. Check the agent above and press Start again.`
      : `The default for ${fam.family} changed and none of its clients can start now — nothing was started.`;
  };
  // A terminal launch opens its session page BEFORE the socket is admitted, so an automatic default
  // is confirmed against the server's CURRENT family decision first (#1728): if it moved, the form
  // stays, says so, and Start is the operator's deliberate retry. The socket still refuses a
  // default that moves in the instant between (4412), and that refusal comes back to this form.
  const startConsoleChecked = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    try {
      const fresh = await reloadRoster();
      if (!mounted.current) return;
      const fam = familyOf(engine);
      if (fresh === "ok" && fam && fam.interactive_default.engine !== engine) {
        refreshConfig();
        setStartError(staleWords(fam));
        return;
      }
    } finally {
      chatStarting.current = false;
      if (mounted.current) setStartingChat(false);
    }
    launchConsole();
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
    if (createChoice === "automatic") {
      void startConsoleChecked();
      return;
    }
    launchConsole();
  };

  const launchConsole = () => {
    const id = mintNewSessionId(engine);
    if (!id) return;
    const fresh = {
      cwd,
      bypass,
      ...(model === "default" ? {} : { model }),
      ...(specialist ? { specialist: { id: specialist.id, version: specialist.version } } : {}),
      ...(createChoice ? { choice: createChoice } : {}),
    };
    noteExplicitPick();
    // Back to the map as a window, when that is where this came from AND the workspace has room
    // under the operator's cap. The request is queued on the workspace and drained by the canvas
    // once it has mounted and measured — the anchor and the overlay box are facts only the map
    // has, so it is the map that opens the window.
    //
    // The `hasRoom` half is not a nicety: at capacity the map would refuse the open and raise a
    // notice, and the launch — with the cwd and bypass choice just made on this form — would be
    // gone (Hermes on #939, finding 2). Starting the session is the operator's actual intent, so
    // a full workspace falls back to the ordinary full-screen launch rather than losing it. On the
    // page it does NOT consult `mapReady` (this form replaced the map, so that is already false);
    // in the map's modal it does, since the map is right there (#1648, `openLaunched`).
    openLaunched({ key: `${engine}:${id}`, engine, id, title: "New session" }, fresh);
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
    // The modal that HOSTS this form (#1648) is not such a dialog: any other one still blocks.
    const own = e.currentTarget.closest('[aria-modal="true"]');
    const modals = Array.from(document.querySelectorAll('[aria-modal="true"]'));
    if (modals.some((m) => m !== own)) return;
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
      <div
        className={styles.form}
        data-new-session-form
        data-creating={startingChat || undefined}
        onKeyDown={onFormKeyDown}
        onClickCapture={holdLinksWhileCreating}
      >
        <div className={styles.scroll}>
        <div className={styles.card}>
        {!inModal && (
          <>
            <div className={styles.brandHero}>
              <div className={styles.wordmark}>
                Battle<b>Lab</b>
              </div>
              <p className={styles.tagline}>Command &amp; Code</p>
            </div>
            <h1>Start a new session</h1>
          </>
        )}

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
              defaultEngine={globalEngine ?? ""}
              projectEngine={projectEngine ?? ""}
              marks={familyMarks}
              onReaffirm={(id) => {
                // Choosing the preselected client explicitly: remembered once it starts, and sent
                // as `explicit`. Model, effort and permission choices stay as they are.
                setEngineChoice(id);
                setTilePicked(true);
              }}
              onChange={(id) => {
                setEngineChoice(id);
                setTilePicked(true);
                dropSpecialist(); // another agent: a plain session
                setModelChoice(null); // a model belongs to the engine it was chosen for
                setApiBypassChoice(null); // a skip choice never survives an agent change (Hermes 5913)
              }}
            />
            {familyPick?.reason === "remembered_explicit" && (
              <p className={styles.hint} data-testid="new-session-remembered">
                Remembered: you last chose <b>{engineLabel(familyPick.engine ?? "")}</b> for{" "}
                {familyPick.family.family} on this device.{" "}
                <button
                  type="button"
                  className={styles.linkButton}
                  data-testid="new-session-use-default"
                  onClick={() => forgetFamilyPick(familyPick.family.family)}
                >
                  {engineInfo(familyPick.family.interactive_default.engine ?? "")?.runtime === "api"
                    ? "Use API"
                    : "Use the default"}
                </button>
              </p>
            )}
            {familyPick?.stale && (
              <p className={styles.hint} role="status" data-testid="new-session-remembered-stale">
                Your last choice for {familyPick.family.family},{" "}
                <b>{engineLabel(familyPick.stale.engine)}</b>, isn’t used: {familyPick.stale.why}.
                {familyPick.engine ? ` Starting ${engineLabel(familyPick.engine)} instead.` : ""}
              </p>
            )}
          </section>
        )}
        {/* The agent catalog, one tap away (#1739): the same Add-agent flow as Settings → Agents. */}
        <div className={styles.addAgentRow}>
          <Link className={`${button.ghost} ${styles.addAgent}`} to={setupPath()} data-testid="new-session-add-agent">
            + Add agent
          </Link>
        </div>

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
            {unavailableDefaultNotice(defaultChoice.unavailableDefault, globalEngine)}
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
            leavesMap={inModal}
          />

          {/* What the project's sessions start with (#1508) — the fields it fills are badged. */}
          {selectedProject && Object.keys(projectDefaults).length > 0 && (
            <p className={styles.hint} data-testid="new-session-project-defaults">
              <b>{selectedProject.name}</b> starts as{" "}
              {describeLaunchDefaults(projectDefaults, {
                engine: globalEngine,
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
                data-during-create=""
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
                <Link to={settingsPath("projects")}>Settings → Projects</Link>
                {inModal && <span className={styles.leavesMap}> ↗ leaves the map</span>}.
              </>
            ) : (
              "Pick a project to start in."
            )}
          </p>
          {mapFull && (
            <p className={styles.mapFull} role="status" data-testid="new-session-map-full">
              <b>The map is full.</b> Close a window to start here, or start it full screen.
            </p>
          )}
          <button
            type="button"
            className={`${styles.start} shine`}
            disabled={!canStart || startingChat}
            onClick={start}
            aria-keyshortcuts="Control+Enter Meta+Enter"
          >
            {startingChat ? "Starting…" : mapFull ? "Start full screen" : "Start session"}
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
