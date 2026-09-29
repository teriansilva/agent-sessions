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
  engineName,
  mintsOwnId,
  resolveDefault,
  useEngineRoster,
} from "../app/engineRoster";
import { unavailableDefaultNotice } from "../lib/agentDefaults";
import { owningProjectId } from "../lib/projectTree";
import type { ProjectEntity } from "../types/api";
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
  const [bypassChoice, setBypass] = useState<boolean | null>(
    restore.draft?.bypassChoice ?? null,
  );
  const bypass = bypassChoice ?? config?.agent_defaults?.bypass ?? true;

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

  const start = () => {
    if (!canStart) return;
    if (engineInfo(engine)?.runtime === "chat") {
      void startChat();
      return;
    }
    const id = mintNewSessionId(engine);
    if (!id) return;
    const fresh = { cwd, bypass };
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
              onChange={(e) => setEngineChoice(e.target.value)}
            >
              {engines.map((id) => (
                <option key={id} value={id}>
                  {engineName(id)}
                </option>
              ))}
            </select>
          </label>
        )}

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

        {/* A `chat` agent (#1209) has no tools, so there are no permission prompts to skip. */}
        {engineInfo(engine)?.runtime !== "chat" && (
        <label className={styles.checkbox}>
          <input
            type="checkbox"
            checked={bypass}
            onChange={(e) => setBypass(e.target.checked)}
          />
          <span>Skip permission prompts</span>
        </label>
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
