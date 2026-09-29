import { useEffect, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useConfigRefresh } from "../app/config";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { ProjectColorPicker } from "../components/ProjectColorPicker";
import { WizardShell } from "../components/wizard/WizardShell";
import { ApiError, api } from "../lib/api";
import { shortCwd } from "../lib/format";
import {
  cancelState,
  finishState,
  readWizardEntry,
  returnTarget,
} from "../lib/newProject";
import { MAP_PATH, MISSION_PATH, SESSIONS_PATH } from "../lib/routes";
import type { FsDir, ProjectEntity } from "../types/api";
import {
  folderOwner,
  folderPath,
  initialDraft,
  isDirty,
  nameClash,
  preselectColor,
  reachable,
  stepIndex,
  STEPS,
  stepValid,
  suggestFolderName,
  type ProjectDraft,
  type StepId,
} from "./newProjectSteps";
import styles from "./NewProject.module.css";

const HEADINGS: Record<StepId, string> = {
  name: "Name the project",
  folder: "Where does it live?",
  colour: "Pick a colour",
  review: "Review and create",
  done: "Project created",
};

type Created = Omit<ProjectEntity, "session_count">;

interface CreateError {
  message: string;
  /** The step that fixes it — the review's error links straight there. `null` when the failure is
   *  not about any one field (a 500, the network, a refused session): no link then. */
  fix: StepId | null;
  /** Set when the folder was made (or already existed) before the project create failed: it is
   *  left in place and named, never called empty or new — mkdir success proves neither. */
  leftover: string | null;
}

const errText = (e: unknown, fallback: string) =>
  e instanceof ApiError && e.message ? e.message : fallback;

/** The New project wizard at `/projects/new` (#1187): NAME → FOLDER → COLOUR → REVIEW → DONE.
 *
 *  Nothing is written before CREATE. A new folder is made at CREATE, not at the FOLDER step, so
 *  Back or leaving never leaves a directory behind; then `POST /api/projects`; then, if asked, the
 *  default-project pref. No new server route — every write is an existing, CSRF-guarded one, and
 *  the server stays the authority on the folder (`$HOME`-bounded, one project per folder → 409).
 *
 *  Where it returns to comes from router state through a closed map (`lib/newProject.ts`). Entered
 *  from New session, finishing goes back there with the new project selected and the form's agent,
 *  bypass and map-return choices intact; cancelling restores the form exactly as it was left. */
export default function NewProject() {
  const location = useLocation();
  const navigate = useNavigate();
  const refreshConfig = useConfigRefresh();
  const [entry] = useState(() => readWizardEntry(location.state));

  const [projects, setProjects] = useState<ProjectEntity[]>([]);
  const [draft, setDraft] = useState<ProjectDraft>(() => initialDraft());
  const [step, setStep] = useState(0);
  const stepId = STEPS[step].id;
  const patch = (p: Partial<ProjectDraft>) => setDraft((d) => ({ ...d, ...p }));

  // Every project, archived ones included: the server's one-project-per-folder rule counts them.
  useEffect(() => {
    api
      .projectEntities({ includeArchived: true })
      .then((r) => {
        setProjects(r.projects);
        setDraft((d) =>
          d.touched.color ? d : { ...d, color: preselectColor(r.projects) },
        );
      })
      .catch(() => {});
  }, []);

  // The new folder's parent defaults to home — the server resolves an empty path to it.
  const [parentDirs, setParentDirs] = useState<FsDir[] | null>(null);
  const [parentError, setParentError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api
      .fsDirs(draft.parent || undefined)
      .then((r) => {
        if (!live) return;
        setParentError(null);
        setParentDirs(r.dirs);
        setDraft((d) => (d.parent ? d : { ...d, parent: r.path }));
      })
      .catch((e) => {
        if (!live) return;
        setParentDirs(null);
        setParentError(errText(e, "Couldn’t read that folder."));
      });
    return () => {
      live = false;
    };
  }, [draft.parent]);

  const [picker, setPicker] = useState<null | "parent" | "existing">(null);
  const [pickerReturn, setPickerReturn] = useState<HTMLElement | null>(null);

  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<CreateError | null>(null);
  const [created, setCreated] = useState<Created | null>(null);
  const [defaultState, setDefaultState] = useState<
    "none" | "setting" | "set" | "failed"
  >("none");

  const path = folderPath(draft);
  const nameTaken = nameClash(draft.name, projects);
  const owner = folderOwner(path, projects);
  const reused =
    draft.folderMode === "new" &&
    !!parentDirs?.some((d) => d.name === draft.folderName.trim());

  // Nothing to lose once the project exists; before that, anything typed is.
  const leaveGuard = !created && (isDirty(draft) || creating);
  const from = entry.from;
  const target = returnTarget(from);

  const go = (i: number) => {
    setCreateError(null);
    setStep(i);
  };

  const cancel = () => {
    if (from === "new-session")
      navigate(SESSIONS_PATH, { replace: true, state: cancelState(entry.draft) });
    else navigate(target ?? SESSIONS_PATH, { replace: true });
  };

  const setDefault = async (id: string) => {
    setDefaultState("setting");
    try {
      await api.setPrefs({ default_project_id: id });
      refreshConfig();
      setDefaultState("set");
    } catch {
      setDefaultState("failed");
    }
  };

  const create = async () => {
    if (creating || !stepValid("review", draft)) return;
    setCreating(true);
    setCreateError(null);
    let folder = path;
    let folderOnDisk: string | null = null;
    try {
      if (draft.folderMode === "new") {
        folder = (await api.fsMkdir(draft.parent, draft.folderName.trim())).path;
        folderOnDisk = folder;
      }
    } catch (e) {
      setCreateError({
        message: errText(e, "Couldn’t create the folder."),
        fix: "folder",
        leftover: null,
      });
      setCreating(false);
      return;
    }
    let project: Created;
    try {
      project = await api.createProject({
        name: draft.name.trim(),
        color: draft.color,
        default_folder: folder,
      });
    } catch (e) {
      // 409 = the folder belongs to another project; a 422 names the field it refused.
      const message = errText(e, "Couldn’t create the project.");
      const field = e instanceof ApiError && (e.status === 409 || e.status === 422);
      const fix: StepId | null =
        field && (e.status === 409 || /folder/i.test(message))
          ? "folder"
          : field && /colou?r/i.test(message)
            ? "colour"
            : field && /name/i.test(message)
              ? "name"
              : null;
      setCreateError({ message, fix, leftover: folderOnDisk });
      setCreating(false);
      return;
    }
    setCreated(project);
    setCreating(false);
    setStep(stepIndex("done"));
    if (draft.makeDefault) void setDefault(project.id);
  };

  // --- DONE's exits: `replace`, so the finished wizard does not linger in history. ---
  const startSession = (id: string) =>
    navigate(SESSIONS_PATH, {
      replace: true,
      state: finishState(from === "new-session" ? entry.draft : null, id),
    });

  const openPicker = (mode: "parent" | "existing", e: { currentTarget: HTMLElement }) => {
    setPickerReturn(e.currentTarget);
    setPicker(mode);
  };

  const editLink = (to: StepId, what: string) => (
    <button
      type="button"
      className={styles.edit}
      aria-label={`Edit ${what}`}
      onClick={() => go(stepIndex(to))}
    >
      Edit
    </button>
  );

  const colourName = (c: string) => (c ? c : "none");

  let body: ReactNode;
  switch (stepId) {
    case "name":
      body = (
        <>
          <p className={styles.lede}>
            A project groups sessions and missions across folders and owns the
            folder new sessions start in.
          </p>
          <label className={styles.field}>
            <span>Project name</span>
            <input
              type="text"
              value={draft.name}
              onChange={(e) => {
                const name = e.target.value;
                setDraft((d) => ({
                  ...d,
                  name,
                  folderName: d.touched.folderName ? d.folderName : suggestFolderName(name),
                }));
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && stepValid("name", draft)) go(step + 1);
              }}
              placeholder="e.g. Payments API"
              maxLength={120}
            />
          </label>
          {nameTaken && (
            <p className={styles.warn} role="status">
              A project called “{nameTaken.name}” already exists. You can still
              use the name.
            </p>
          )}
        </>
      );
      break;
    case "folder":
      body = (
        <>
          <div className={styles.modes} role="radiogroup" aria-label="Folder">
            {(
              [
                ["new", "New folder", "Make a folder under a parent you choose"],
                ["existing", "Existing folder", "Use a folder that is already there"],
              ] as const
            ).map(([mode, label, hint]) => (
              <label
                key={mode}
                className={draft.folderMode === mode ? `${styles.mode} ${styles.modeOn}` : styles.mode}
              >
                <input
                  type="radio"
                  name="folder-mode"
                  checked={draft.folderMode === mode}
                  onChange={() => patch({ folderMode: mode })}
                />
                <span>
                  <b>{label}</b>
                  <small>{hint}</small>
                </span>
              </label>
            ))}
          </div>
          {draft.folderMode === "new" ? (
            <>
              <div className={styles.field}>
                <span id="np-parent-label">Parent folder</span>
                <div className={styles.pathRow}>
                  <code className={styles.path} aria-labelledby="np-parent-label">
                    {draft.parent ? shortCwd(`${draft.parent}/`).replace(/(.)\/$/, "$1") : "…"}
                  </code>
                  <button
                    type="button"
                    className={styles.ghostBtn}
                    onClick={(e) => openPicker("parent", e)}
                  >
                    Change…
                  </button>
                </div>
              </div>
              <label className={styles.field}>
                <span>Folder name</span>
                <input
                  type="text"
                  value={draft.folderName}
                  onChange={(e) =>
                    setDraft((d) => ({
                      ...d,
                      folderName: e.target.value,
                      touched: { ...d.touched, folderName: true },
                    }))
                  }
                  placeholder="folder-name"
                  spellCheck={false}
                  autoCapitalize="off"
                />
              </label>
              {parentError && <p className={styles.error}>{parentError}</p>}
              {draft.folderName.trim() && !path && (
                <p className={styles.error}>
                  A folder name is one path segment — no “/”, and not “.” or “..”.
                </p>
              )}
              {path && (
                <p className={styles.preview} data-testid="np-folder-preview">
                  <code>{shortCwd(path)}</code>{" "}
                  <span className={reused ? styles.tagWarn : styles.tag}>
                    {reused ? "existing folder, reused" : "created at the end"}
                  </span>
                </p>
              )}
              {reused && (
                <p className={styles.warn} role="status">
                  A folder with that name is already in the parent. The project
                  will use it as it is — nothing in it is changed.
                </p>
              )}
            </>
          ) : (
            <div className={styles.field}>
              <span id="np-existing-label">Folder</span>
              <div className={styles.pathRow}>
                <code className={styles.path} aria-labelledby="np-existing-label">
                  {draft.existingPath ? shortCwd(draft.existingPath) : "no folder chosen"}
                </code>
                <button
                  type="button"
                  className={styles.ghostBtn}
                  onClick={(e) => openPicker("existing", e)}
                >
                  Choose folder…
                </button>
              </div>
            </div>
          )}
          {owner && (
            <p className={styles.warn} role="status" data-testid="np-folder-owner">
              This folder overlaps a folder of the project “{owner.name}”
              {owner.archived ? " (archived)" : ""}. A folder belongs to one
              project, so creating will be refused — pick another folder.
            </p>
          )}
        </>
      );
      break;
    case "colour":
      body = (
        <>
          <p className={styles.lede}>
            The colour marks the project in the session list and on the map.
          </p>
          <ProjectColorPicker
            label="Project colour"
            value={draft.color}
            size="large"
            clearLabel="None"
            onChange={(color) =>
              setDraft((d) => ({ ...d, color, touched: { ...d.touched, color: true } }))
            }
          />
          <div className={styles.previews} aria-label="Preview">
            <div className={styles.previewRow} data-testid="np-preview-row">
              <span
                className={draft.color ? styles.dot : `${styles.dot} ${styles.dotEmpty}`}
                style={draft.color ? { background: draft.color } : undefined}
                aria-hidden="true"
              />
              <span className={styles.previewName}>{draft.name.trim() || "Project"}</span>
              <span className={styles.previewMeta}>session list</span>
            </div>
            <div
              className={styles.previewCluster}
              style={draft.color ? { borderColor: draft.color } : undefined}
            >
              <span
                className={styles.previewClusterHead}
                style={draft.color ? { color: draft.color } : undefined}
              >
                {draft.name.trim() || "Project"}
              </span>
              <span className={styles.previewMeta}>map</span>
            </div>
          </div>
        </>
      );
      break;
    case "review":
      body = (
        <>
          <dl className={styles.review}>
            <div>
              <dt>Name</dt>
              <dd>{draft.name.trim()}</dd>
              {editLink("name", "name")}
            </div>
            <div>
              <dt>Folder</dt>
              <dd>
                <code>{shortCwd(path)}</code>{" "}
                <span className={styles.tag}>
                  {draft.folderMode === "existing" || reused
                    ? "existing"
                    : "created if absent"}
                </span>
              </dd>
              {editLink("folder", "folder")}
            </div>
            <div>
              <dt>Colour</dt>
              <dd>
                <span
                  className={draft.color ? styles.dot : `${styles.dot} ${styles.dotEmpty}`}
                  style={draft.color ? { background: draft.color } : undefined}
                  aria-hidden="true"
                />{" "}
                {colourName(draft.color)}
              </dd>
              {editLink("colour", "colour")}
            </div>
          </dl>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={draft.makeDefault}
              onChange={(e) => patch({ makeDefault: e.target.checked })}
            />
            <span>Make this my default project for new sessions</span>
          </label>
          {createError && (
            <div className={styles.failure} role="alert" data-testid="np-create-error">
              <p>{createError.message}</p>
              {createError.leftover && (
                <p>
                  The folder <code>{shortCwd(createError.leftover)}</code> exists
                  and stays where it is. Trying again is safe.
                </p>
              )}
              {createError.fix && (
                <button
                  type="button"
                  className={styles.edit}
                  onClick={() => go(stepIndex(createError.fix!))}
                >
                  Change the {createError.fix}
                </button>
              )}
            </div>
          )}
        </>
      );
      break;
    case "done":
      body = created && (
        <>
          <p className={styles.lede}>
            <span
              className={created.color ? styles.dot : `${styles.dot} ${styles.dotEmpty}`}
              style={created.color ? { background: created.color } : undefined}
              aria-hidden="true"
            />{" "}
            <b>{created.name}</b> starts sessions in{" "}
            <code>{shortCwd(created.default_folder)}</code>.
          </p>
          {defaultState === "set" && (
            <p className={styles.ok} role="status">
              It is now your default project.
            </p>
          )}
          {defaultState === "failed" && (
            <div className={styles.failure} role="alert">
              <p>Created, but not set as your default project.</p>
              <button
                type="button"
                className={styles.edit}
                onClick={() => void setDefault(created.id)}
              >
                Try again
              </button>
            </div>
          )}
          <div className={styles.doneActions}>
            <button
              type="button"
              className={styles.primaryBtn}
              onClick={() => startSession(created.id)}
            >
              Start a session
            </button>
            <button
              type="button"
              className={styles.ghostBtn}
              onClick={() =>
                navigate(MISSION_PATH, {
                  replace: true,
                  state: { missionProjectId: created.id },
                })
              }
            >
              Plan a mission here
            </button>
            <button
              type="button"
              className={styles.ghostBtn}
              onClick={() => navigate(MAP_PATH, { replace: true })}
            >
              Show on the map
            </button>
            {target && (
              <button
                type="button"
                className={styles.ghostBtn}
                onClick={() =>
                  from === "new-session"
                    ? startSession(created.id)
                    : navigate(target, { replace: true })
                }
              >
                Done
              </button>
            )}
          </div>
        </>
      );
      break;
  }

  const isReview = stepId === "review";
  const isDone = stepId === "done";

  return (
    <>
      <WizardShell
        kicker="New project"
        steps={STEPS}
        current={step}
        canJump={isDone ? () => false : (i) => reachable(i, draft)}
        onJump={go}
        heading={HEADINGS[stepId]}
        onBack={step > 0 && !isDone ? () => go(step - 1) : undefined}
        onNext={
          isDone ? undefined : isReview ? () => void create() : () => go(step + 1)
        }
        nextLabel={isReview ? (creating ? "Creating…" : "Create project") : "Next"}
        nextDisabled={creating || !stepValid(stepId, draft)}
        secondary={
          isDone ? undefined : (
            <button type="button" className={styles.ghostBtn} onClick={cancel}>
              Cancel
            </button>
          )
        }
        leaveGuard={leaveGuard}
        leaveTitle={draft.name.trim() || "New project"}
        {...(creating
          ? {
              // Leaving does not stop a create already on the wire — say so, don't say "discard".
              leaveMessage:
                "The project is being created. Leaving now does not stop that: it will still be created.",
              leaveConfirmLabel: "Leave anyway",
            }
          : {})}
      >
        {body}
      </WizardShell>
      {picker && (
        <FolderPickerModal
          initialPath={
            picker === "parent" ? draft.parent || undefined : draft.existingPath || undefined
          }
          title={picker === "parent" ? "Choose the parent folder" : "Choose the project's folder"}
          onPick={(p) => {
            if (picker === "parent")
              setDraft((d) => ({ ...d, parent: p, touched: { ...d.touched, parent: true } }));
            else patch({ existingPath: p });
            setPicker(null);
          }}
          onCancel={() => setPicker(null)}
          returnFocusTo={pickerReturn}
        />
      )}
    </>
  );
}
