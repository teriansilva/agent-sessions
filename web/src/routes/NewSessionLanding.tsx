import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { MAP_PATH, useMapWindows } from "../app/workspaceWindows";
import { FolderPickerModal } from "../components/FolderPickerModal";
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
import { owningProjectId } from "../lib/projectTree";
import type { ProjectEntity, StructuredModelList } from "../types/api";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project + folder and start a new
 *  session (#448): a project owns a DEFAULT launch folder, so choosing a project prefills the
 *  folder; the folder is overridable for this one session via a ~/-rooted picker. We mint a
 *  client-side id, navigate to /s/:engine/:id carrying the fresh-launch params (cwd + bypass).
 *
 *  Unless the map sent us (#936): pressing "+ New session" with the workspace up carries
 *  `returnTo: "/overview"` in router state, and then the launch goes back to the MAP as a window
 *  rather than taking over the screen. The form itself stays a page either way — it wants the
 *  room, and a 720×480 window has none to spare. */
/** The structured create whose outcome is unknown (#1311), per tab. */
const PENDING_CREATE = "battlelab.pendingStructuredCreate";
/** `model` (#1313) and `bypass` (#1339) are part of the create's identity: a retry reuses the id
 *  only for the same choices, so a reload can never resend an id with a different one. */
type PendingCreate = { engine: string; cwd: string; model?: string; id: string; bypass?: boolean };

function readPendingCreate(): PendingCreate | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(PENDING_CREATE) ?? "null") as PendingCreate | null;
    return v &&
      typeof v.engine === "string" &&
      typeof v.cwd === "string" &&
      typeof v.id === "string" &&
      (v.bypass === undefined || typeof v.bypass === "boolean")
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
  // An API client's skip choice is SEPARATE and BOUND to the engine it was ticked for (#1339,
  // Hermes on #1341): a console choice never carries into an API client, switching agents always
  // starts guarded, and it is never restored from a draft — only a tick on this form, for this
  // client, skips its prompts. The tick is never restored or replayed — not on reload, not for a
  // retry (Hermes on #1341, rounds 1–2): an unresolved create is resolved by ASKING THE SERVER
  // below, never by re-sending a skip the operator did not just tick.
  const [apiBypassChoice, setApiBypassChoice] = useState<{ engine: string; value: boolean } | null>(
    null,
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
    // The lookup only OFFERS it; the slot is discharged when the operator opens it (below), never
    // from here — an unmounted or late callback must not erase the only pointer (Hermes 5920).
    api
      .structuredSnapshot(`${p.engine}:${p.id}`)
      .then(() => {
        if (live) setRecovered(p);
      })
      .catch(() => {
        // Never made (or not yet): the guarded attempt keeps its id for a harmless same-form retry.
      });
    return () => {
      live = false;
    };
  }, []);

  const roster = useEngineRoster();
  const engines = config?.new_session_engines ?? [];
  // The stored default agent, resolved through THE resolver (#1128): used when it can start a new
  // session here, else the first engine that can — and the notice below says so.
  const defaultChoice = resolveDefault(
    roster.engines.filter((e) => engines.includes(e.id)),
    config?.agent_defaults?.default_engine,
    "new",
  );
  const engine = engineChoice || defaultChoice.engine || engines[0] || "";
  const runtimeOf = (id: string) => engineInfo(id)?.runtime;
  const apiEngines = engines.filter((id) => runtimeOf(id) === "api" || runtimeOf(id) === "chat");
  const consoleEngines = engines.filter((id) => !apiEngines.includes(id));
  const unavailable = config?.unavailable_clients ?? [];
  const isApi = runtimeOf(engine) === "api";
  const apiSource = engineInfo(engine)?.api?.source ?? "";
  const apiCanBypass = isApi && engineInfo(engine)?.api?.can_bypass === true;
  // An API client starts GUARDED unless ticked on this form (#1339): the operator approved the
  // option, not a default-on posture, so the stored console default does not apply to it.
  const bypass = isApi
    ? apiBypassChoice?.engine === engine && apiBypassChoice.value
    : (bypassChoice ?? config?.agent_defaults?.bypass ?? true);
  const setBypass = (value: boolean) =>
    isApi ? setApiBypassChoice({ engine, value }) : setConsoleBypass(value);

  // The model (#1189), BOUND to the engine it was chosen for: switching engine — by the select or
  // by the default resolving differently — reads as `default` again, never as a model the other
  // engine happens to share a name with. A choice the server no longer offers is sent as chosen and
  // refused there (4422), never quietly swapped for `default` here.
  const [modelChoice, setModelChoice] = useState<{ engine: string; model: string } | null>(
    restore.draft?.modelChoice ?? null,
  );
  const model = modelChoice?.engine === engine ? modelChoice.model : "default";
  // A native API client's models come from its own CLI (#1313), asked when it is selected; a
  // console agent's from the roster. Each option is what is sent (`id`) and what is shown.
  const [apiModels, setApiModels] = useState<{ engine: string; list: StructuredModelList } | null>(
    null,
  );
  useEffect(() => {
    if (!isApi) return;
    let live = true;
    api
      .structuredModels(engine)
      .then((list) => live && setApiModels({ engine, list }))
      .catch((e) => {
        if (!live) return;
        const reason = e instanceof ApiError && e.message ? e.message : "it could not be asked";
        setApiModels({ engine, list: { status: "unavailable", models: [], reason } });
      });
    return () => {
      live = false;
    };
  }, [engine, isApi]);
  const apiList = isApi && apiModels?.engine === engine ? apiModels.list : null;
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

  // Project entities own the default launch folder (#448). projectChoice === null = untouched
  // (use the default selection); "" = no project; else an entity id.
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  // A created project wins; otherwise the draft's own choice comes back in all three states.
  const [projectChoice, setProjectChoice] = useState<string | null>(
    restore.selectProjectId ?? restore.draft?.projectChoice ?? null,
  );
  // The operator's starred project (#615 Phase 2). `entities` is already archived-filtered, so an
  // id naming an archived — or since-deleted — project simply isn't found, and we fall back to the
  // first project rather than pre-selecting nothing. Before #615 there was no pref at all: the
  // preselection was `entities[0]`, i.e. whichever project sorted first by name.
  const starredId = config?.default_project_id ?? "";
  const starredExists = entities.some((p) => p.id === starredId);
  const defaultProjectId = (starredExists ? starredId : entities[0]?.id) ?? "";
  const projectSel = projectChoice ?? defaultProjectId;
  const selectedProject = entities.find((p) => p.id === projectSel);

  // Folder: the project's default unless overridden for this session via the picker.
  // A created project brings its own folder, so a finish never restores the old override.
  const [cwdOverride, setCwdOverride] = useState<string | null>(
    restore.selectProjectId ? null : (restore.draft?.cwdOverride ?? null),
  );

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
  // `||`, not `??`, between the project's folder and the legacy pref: a folderless project stores
  // "" (#448 back-compat), and "" is a *missing* folder, not a chosen one — fall through to the
  // legacy cwd rather than opening the picker on nothing (#615 Phase 2 edge case). `cwdOverride`
  // keeps `??`: an explicit "" from the picker is a real choice.
  const projectCwd =
    selectedProject?.default_folder || config?.default_project || "";
  const cwd = cwdOverride ?? projectCwd;
  const isProjectDefault =
    !!selectedProject && cwd === selectedProject.default_folder && cwd !== "";

  // The folder picker overrides this session's folder. `false` = closed.
  const [picker, setPicker] = useState(false);
  // Captured at open time (not read from a ref during render) so focus returns to the trigger.
  const [pickerReturn, setPickerReturn] = useState<HTMLElement | null>(null);
  const openPicker = (e: { currentTarget: HTMLElement }) => {
    setPickerReturn(e.currentTarget);
    setPicker(true);
  };

  // Creating a project is the New project wizard's job (#1187). It carries this form's draft so
  // finishing — or cancelling — brings the form back exactly as it is now.
  const draft: NewSessionDraft = {
    engineChoice,
    bypassChoice,
    returnTo: returnToMap ? MAP_PATH : null,
    projectChoice,
    cwdOverride,
    ...(modelChoice ? { modelChoice } : {}),
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

  const refreshEntities = () =>
    api
      .projectEntities()
      .then((r) => setEntities(r.projects.filter((p) => !p.archived)))
      .catch(() => {});

  useEffect(() => {
    refreshEntities();
  }, []);

  // Nothing launches on a guessed capability (#853 P4): until the roster says whether this engine
  // pins or adopts its id, Start waits. `roster` is read so this re-evaluates when it lands.
  const mintKnown = roster.loaded && mintsOwnId(engine) !== undefined;
  const canStart = Boolean(engine && cwd && mintKnown);

  const [startError, setStartError] = useState<string | null>(null);
  // Chat creation is single-flight (Hermes on #1219): each `/api/chat/new` mints and persists a
  // conversation, so a second click while one is pending would create a duplicate. The ref is
  // the reservation (set before the await, so a same-tick double click is caught); the state
  // disables the button. `mounted` stops a late completion from navigating after the operator
  // has left the form.
  const chatStarting = useRef(false);
  const [startingChat, setStartingChat] = useState(false);
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
    // A skip create keeps NO client recovery slot (Hermes 5913): with the two-phase start a lost
    // one can never run, and the server lists it — its view offers Start / Discard until it
    // expires. Only a guarded create reuses an unresolved id.
    const attempt =
      !skip &&
      prev &&
      !prev.bypass &&
      prev.engine === engine &&
      prev.cwd === cwd &&
      (prev.model ?? "default") === model
        ? prev
        : { engine, cwd, model, id: crypto.randomUUID(), bypass: skip };
    if (skip) {
      if (prev?.bypass) writePendingCreate(null);
    } else {
      writePendingCreate(attempt);
    }
    const clearAttempt = () => {
      if (readPendingCreate()?.id === attempt.id) writePendingCreate(null);
    };
    try {
      const { session_key: key } = await api.structuredCreate(
        engine,
        cwd,
        attempt.id,
        model,
        Boolean(attempt.bypass),
      );
      clearAttempt(); // only THIS attempt's slot — a lost guarded one stays recoverable (5916)
      if (attempt.bypass) {
        // Phase 2 (#1339): only now — the create's response is in hand — does it launch. A lost
        // start is safe: the session view shows Start / Discard until it is confirmed.
        await api.structuredStart(key).catch(() => {});
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
      if (e instanceof ApiError && e.status < 500) clearAttempt(); // refused
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
        // One tick authorizes ONE create (Hermes on #1341, review 5876): after any skip attempt,
        // whatever its outcome, a further skip create needs a fresh tick.
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
    const fresh = model === "default" ? { cwd, bypass } : { cwd, bypass, model };
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

  const onPick = (path: string) => {
    setCwdOverride(path);
    setPicker(false);
  };

  return (
    <div className={styles.landing}>
      <div className={styles.card}>
        <div className={styles.brandHero}>
          <div className={styles.wordmark}>
            Battle<b>Lab</b>
          </div>
          <p className={styles.tagline}>Command &amp; Code</p>
        </div>
        <h1>Start a new session</h1>

        {engines.length > 1 && (
          <label className={styles.field}>
            <span>Agent</span>
            <select
              value={engine}
              onChange={(e) => {
                setEngineChoice(e.target.value);
                setModelChoice(null); // a model belongs to the engine it was chosen for
                setApiBypassChoice(null); // a skip tick never survives an agent change (Hermes 5913)
              }}
            >
              {/* Console (a terminal) vs API (structured, no terminal) — #1311. A client that
                  cannot start is listed, disabled, so its absence is explained, not silent. */}
              <optgroup label="Console — terminal">
                {consoleEngines.map((id) => (
                  <option key={id} value={id}>
                    {engineName(id)}
                  </option>
                ))}
              </optgroup>
              {(apiEngines.length > 0 || unavailable.length > 0) && (
                <optgroup label="API — structured, no terminal">
                  {apiEngines.map((id) => (
                    <option key={id} value={id}>
                      {engineLabel(id)}
                    </option>
                  ))}
                  {unavailable.map((c) => (
                    <option key={c.id} value={`unavailable:${c.id}`} disabled>
                      {c.label} — unavailable
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
        )}
        {isApi && (
          <div className={styles.about} data-testid="new-session-api-about">
            <div className={styles.aboutHead}>
              <span className={`${styles.led} ${styles.ledUp}`} aria-hidden="true" />
              API client · ready
            </div>
            <p>
              BattleLab drives your installed <b>{engineLabel(apiSource)}</b> CLI through its
              structured protocol: its login, config, MCP servers and skills apply. No terminal.
              {apiCanBypass
                ? " You answer each request it makes in the session view, unless you skip permission prompts below."
                : " You answer each request it makes in the session view."}
            </p>
          </div>
        )}
        {unavailable.length > 0 && (
          <div className={`${styles.about} ${styles.unavailable}`} data-testid="new-session-unavailable">
            {unavailable.map((c) => (
              <p key={c.id}>
                <span className={`${styles.led} ${styles.ledDown}`} aria-hidden="true" />
                <b>{c.label}</b> is unavailable: <span className={styles.reason}>{c.reason}</span>
              </p>
            ))}
          </div>
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
            <span>Model</span>
            <select
              aria-label="Model"
              data-testid="new-session-model"
              value={model}
              onChange={(e) => setModelChoice({ engine, model: e.target.value })}
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

        {roster.loaded && defaultChoice.unavailableDefault && (
          <p className={styles.hint} role="status">
            {unavailableDefaultNotice(
              defaultChoice.unavailableDefault,
              defaultChoice.engine,
            )}
          </p>
        )}

        {/* Project FIRST (#448): it owns the default launch folder below. */}
        {entities.length > 0 && (
          <label className={styles.field}>
            <span>Project</span>
            <select
              aria-label="Project"
              value={projectSel}
              onChange={(e) => {
                setProjectChoice(e.target.value);
                setCwdOverride(null); // folder follows the newly-selected project's default
              }}
            >
              <option value="">no project</option>
              {entities.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <Link
          to={NEW_PROJECT_PATH}
          state={wizardState}
          className={styles.setDefault}
          onClick={(e) => void openWizard(e)}
        >
          + New project…
        </Link>

        {/* Folder BELOW the project (#448): prefilled from the project's default, overridable. */}
        <label className={styles.field}>
          <span>Folder</span>
          <div className={styles.folderRow}>
            <input
              type="text"
              readOnly
              aria-label="Launch folder"
              className={styles.folderPath}
              value={cwd || "no folder selected"}
            />
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={openPicker}
            >
              Choose folder…
            </button>
          </div>
        </label>
        {isProjectDefault ? (
          <p className={styles.hint}>
            ✓ default folder for “{selectedProject?.name}” — change it just for
            this session
          </p>
        ) : selectedProject &&
          !selectedProject.default_folder &&
          !cwdOverride ? (
          <p className={styles.error}>
            “{selectedProject.name}” has no default folder — choose one for this
            session.
          </p>
        ) : null}

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
          <span>Skip permission prompts</span>
        </label>
        )}
        {isApi && apiCanBypass && bypass && (
          <p className={styles.hint} data-testid="api-bypass-warning">
            {engineLabel(apiSource)} won’t ask before running commands or editing files, and
            runs without its sandbox. Fixed for this session.
          </p>
        )}
        {recovered && (
          <p className={styles.hint} role="status" data-testid="api-recovered-create">
            An earlier start of {engineLabel(recovered.engine)}
            {recovered.bypass ? " with Skip permission prompts" : ""} did create a session.{" "}
            <Link
              to={`/s/${recovered.engine}/${recovered.id}`}
              onClick={() => {
                // Discharged only now, and only if the slot still points at THIS session.
                if (readPendingCreate()?.id === recovered.id) writePendingCreate(null);
              }}
            >
              Open it
            </Link>
          </p>
        )}

        <button
          type="button"
          className={`${styles.start} shine`}
          disabled={!canStart || startingChat}
          onClick={start}
        >
          Start session
        </button>
        {startError && (
          <p className={styles.hint} role="alert" data-testid="start-error">
            {startError}
          </p>
        )}
        <p className={styles.hint}>
          …or open an existing session from the list.
        </p>
      </div>

      {picker && (
        <FolderPickerModal
          initialPath={cwd || undefined}
          title="Choose a folder"
          onPick={onPick}
          onCancel={() => setPicker(false)}
          returnFocusTo={pickerReturn}
        />
      )}
    </div>
  );
}
