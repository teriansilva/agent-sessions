/** NEW MISSION — the brief, the project and START (#948, #967).
 *
 *  It was the `creating` half of `Composer`, which carried a `NEW MISSION | ASK` segmented control
 *  because the two modes shared one box. #1058 gave Ask its own route (`/ask`), so there is no
 *  second mode here to switch to and no mode strip in the footer: the mission landing IS this form.
 *  Everything else is unchanged, including the fence below, which has nothing to do with the mode
 *  and everything to do with a create outliving the view that started it.
 *
 *  The project list loads lazily: an install with no projects is a real state and says so, rather
 *  than offering an empty `<select>` that silently posts nothing. The project is REQUIRED here —
 *  START stays disabled without one. The server will take a project-less create and make a `draft`,
 *  but a mission that cannot run until somebody notices is not what an operator pressing START
 *  asked for, and the one moment they are looking at a picker is the cheapest moment to answer it.
 *
 *  **The project is an ENTITY id, never a folder cwd.** `POST /api/missions` resolves the working
 *  directory server-side from `project_id` against the project store, and rejects a client-sent
 *  `cwd` outright (422). A folder row's id IS its cwd, so sending one here is a 404 "unknown
 *  project" — the two pickers are not interchangeable, which is why this reads
 *  `api.projectEntities()` and not `api.folders()`.
 */
import { BookMarked } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { useConfig } from "../../app/config";
import { api, ApiError } from "../../lib/api";
import type { Mission, ProjectEntity } from "../../types/api";

import action from "../ui/actionButton.module.css";
import styles from "./mission.module.css";
import { renderTemplate } from "../../lib/templateMessage";
import { TemplatePickerModal } from "../templates/TemplatePickerModal";

/** `missions.PLAYBOOK_DECLINED`: this mission gets no checklist — notes only (#1061). */
export const PLAYBOOK_DECLINED = ":none";

/** The server's instruction cap (`missions.INSTRUCTION_MAX`). It truncates silently, so the form
 *  refuses to start over it rather than letting the tail of a brief disappear (#948). */
const INSTRUCTION_MAX = 8000;

export function NewMissionForm({
  onCreated,
  visit,
  isVisitCurrent,
  focusKey,
}: {
  /** `focus` says whether the console should SELECT the new mission.
   *
   *  A create that resolves after the operator moved on still has to refresh the
   *  rail — the mission exists, and hiding it would be worse than showing it — but it must not
   *  steal the selection. Those are two different things and were one before (#896 review 2). */
  onCreated: (m: Mission, opts: { focus: boolean }) => void;
  /** The visit this create belongs to, captured when START is pressed. */
  visit: () => number;
  /** Is that visit still the one on screen? Read at RESOLUTION time, never captured. */
  isVisitCurrent: (at: number) => boolean;
  /** Bumped by "+ New mission" to move focus into the brief (#948). */
  focusKey?: number;
}) {
  /** Focus the field the operator just asked for — NOT ON ARRIVAL (#948). The landing IS this
   *  form, and focusing the brief the moment the section is entered would pop a phone's keyboard
   *  over the page the operator just arrived on. Focus moves when they ASK for it: "+ New mission"
   *  bumps `focusKey` and MOUNTS a fresh form, so a request made before mount would be lost if the
   *  first run were always skipped — hence the `focusKey` test inside the first-run branch rather
   *  than an unconditional early return. Not `setState`, so no cascading render. */
  const arrived = useRef(false);
  useEffect(() => {
    if (!arrived.current) {
      arrived.current = true;
      if (!focusKey) return;
    }
    const raf = requestAnimationFrame(() => {
      document
        .querySelector<HTMLTextAreaElement>(
          '[data-testid="new-mission-instruction"]',
        )
        ?.focus();
    });
    return () => cancelAnimationFrame(raf);
  }, [focusKey]);

  const [instruction, setInstruction] = useState("");
  const [projectId, setProjectId] = useState("");
  /** The checklist for THIS mission (#1061). `null` = untouched, so the pre-selection follows the
   *  configured default even when the config lands after mount; a pick is the operator's and
   *  sticks. */
  const [pickedPlaybook, setPickedPlaybook] = useState<string | null>(null);
  const config = useConfig();
  const playbookBlock = config?.mission_playbooks;
  const playbooks = playbookBlock?.playbooks ?? [];
  const defaultPlaybook = playbooks.some(
    (p) => p.id === playbookBlock?.default_id,
  )
    ? playbookBlock!.default_id
    : PLAYBOOK_DECLINED;
  const playbookId =
    pickedPlaybook !== null &&
    (pickedPlaybook === PLAYBOOK_DECLINED ||
      playbooks.some((p) => p.id === pickedPlaybook))
      ? pickedPlaybook
      : defaultPlaybook;
  const [projects, setProjects] = useState<ProjectEntity[] | null>(null);
  /** The list could not be READ — a different fact from an empty one, and with a different fix. */
  const [projectsError, setProjectsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The template picker (#948) — INSERT-ONLY here: a mission has no session to send into, so a
   *  template becomes part of the brief and the operator still presses Start. */
  const [templatesOpen, setTemplatesOpen] = useState(false);
  /** Where focus returns when the picker closes — the Template button, recorded when it is pressed
   *  (state rather than a ref read during render). */
  const [templatesTrigger, setTemplatesTrigger] = useState<HTMLElement | null>(null);
  const noteId = useId();
  const length = instruction.trim().length;
  const overCap = length > INSTRUCTION_MAX;
  /** False once this form has unmounted — which selecting a mission, or leaving the section, does.
   *  A create is not abortable, so the completion has to be fenced rather than the request
   *  cancelled; `/ask` fences its own answers the same way, for the same reason (#1058). */
  const liveRef = useRef(true);
  useEffect(
    () => () => {
      liveRef.current = false;
    },
    [],
  );

  useEffect(() => {
    let live = true;
    api
      .projectEntities()
      .then((r) => {
        if (!live) return;
        setProjectsError(false);
        setProjects(r.projects ?? []);
      })
      // A LIST THAT COULD NOT BE READ IS NOT AN EMPTY LIST (#896 review 9, finding 2). Both used
      // to render "No projects yet", which sends the operator to Settings to add a project they
      // already have. The two need different fixes, so they say different things.
      .catch(() => {
        if (!live) return;
        setProjectsError(true);
        setProjects([]);
      });
    return () => {
      live = false;
    };
  }, []);

  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const text = instruction.trim();
      // `overCap` is checked HERE, not only on the Start button: Ctrl/⌘+Enter calls
      // `requestSubmit()`, which submits a form whose submit button is disabled, and the server
      // would silently truncate the brief.
      if (!text || busy || !projectId || overCap) return;
      // CAPTURED HERE, not asked for later. The token names the view AND the scope as they were
      // when the operator pressed START (#896 review 10, finding 4).
      const at = visit();
      setBusy(true);
      setError(null);
      try {
        // `cwd` is NEVER sent — the route refuses it (422) and resolves the path itself from the
        // entity. `project_id` is the entity id, not a folder cwd.
        const m = await api.createMission({
          instruction: text,
          project_id: projectId,
          // SENT EXPLICITLY whenever there is anything to choose, so the mission records the
          // checklist the operator saw selected — including "No checklist" — instead of whatever
          // the Settings default becomes later. With no playbooks configured nothing is sent and
          // the server's own default applies, exactly as before.
          ...(playbooks.length ? { playbook_id: playbookId } : {}),
        });
        // FENCED AT RESOLUTION, ON THE VISIT. The operator may have moved to another mission, or
        // flipped Active → Archived while this was in flight. The last of
        // those is the case an id fence cannot see: this form sits on the landing (it sat in the
        // untracked view before #948), which the scope flip does not unmount, so "the same view
        // is showing" stayed true while the rail underneath it became a different set (#896
        // review 10, finding 4).
        //
        // The mission still EXISTS, so the rail is refreshed either way; only the focus is
        // withheld. Dropping it entirely would leave a real mission invisible until the next poll.
        const focus = liveRef.current && isVisitCurrent(at);
        setInstruction("");
        setProjectId("");
        setPickedPlaybook(null);
        onCreated(m, { focus });
      } catch (err) {
        // The server's own `detail` — a project with no folder to work in, an unresolvable
        // project, a refused `cwd`. `mutateJson` carries it precisely so the operator is told
        // which, instead of "POST /api/missions → 422" (#834).
        setError(
          err instanceof ApiError && err.message
            ? err.message
            : "That mission could not be started.",
        );
      } finally {
        setBusy(false);
      }
    },
    [
      instruction,
      projectId,
      busy,
      overCap,
      onCreated,
      visit,
      isVisitCurrent,
      playbooks.length,
      playbookId,
    ],
  );

  /* SAID BEFORE THE CREATE, and it names the fix rather than a control that does not exist.
     The earlier copy said "pick one before it can run", which was a promise this console
     cannot keep: a mission created without a project has no cwd, the server then refuses
     `running` for ever, and there is no assignment path to reach afterwards (#896 review 9,
     finding 2). So a project is REQUIRED where one can be offered, and where none can be —
     an empty list, or one that would not load — the message says which and what to do.

     THE ORDINARY HINT IS THE PICKER'S DESCRIPTION, NOT A LINE (#967). "Pick the project…" only
     restated what the empty option already says ("Choose a project"), so it is kept for assistive
     technology as the select's `aria-describedby` and hidden visually. The other two are states
     with a fix, so they stay visible under the box. */
  const note =
    projects !== null && !projectId
      ? projectsError
        ? "The project list could not be read, so there is nothing to start this in. Try again in a moment."
        : projects.length === 0
          ? "No projects yet. Add one in Settings → Projects, then start the mission here."
          : "Pick the project this mission works in. It decides where its agent runs."
      : null;
  const noteVisible = projectsError || projects?.length === 0;

  return (
    <>
      <form
        className={`${styles.composerBox} ${styles.newMissionForm}`}
        onSubmit={submit}
        data-testid="new-mission-form"
        aria-label="Start a new mission"
      >
        <textarea
          className={`${styles.composerInput} ${styles.boxInput}`}
          rows={1}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          // Enter is a newline — a brief is prose. Ctrl/⌘+Enter starts it.
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          aria-keyshortcuts="Control+Enter Meta+Enter"
          placeholder="Describe the outcome you want — e.g. fix the flaky upload retry and open a PR"
          aria-label="Mission instruction"
          data-testid="new-mission-instruction"
        />
        {/* THE FOOTER ROW, inside the box: mode, project, Template, a spacer, the shortcut and Start
            (#967). Two groups so a phone can wrap it into [mode][project] / [Template][Start]. */}
        <div className={styles.composerFoot} data-testid="composer-foot">
          <div className={styles.footLead}>
            <select
              className={styles.newMissionProject}
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              aria-label="Project"
              aria-describedby={note ? noteId : undefined}
              data-testid="new-mission-project"
            >
              <option value="">
                {projects === null
                  ? "Loading projects…"
                  : projectsError
                    ? "The project list could not be read"
                    : projects.length === 0
                      ? "No projects yet"
                      : "Choose a project"}
              </option>
              {(projects ?? []).map((pr) => (
                <option key={pr.id} value={pr.id}>
                  {pr.name}
                </option>
              ))}
            </select>
            {/* WHICH CHECKLIST, per mission (#1061). Hidden when no playbook is configured: there
                is nothing to choose, and an empty picker would only say so in the way. */}
            {playbooks.length ? (
              <select
                className={`${styles.newMissionProject} ${styles.newMissionPlaybook}`}
                value={playbookId}
                onChange={(e) => setPickedPlaybook(e.target.value)}
                aria-label="Checklist"
                title="The objectives this mission is checked against"
                data-testid="new-mission-playbook"
              >
                {playbooks.map((pb) => (
                  <option key={pb.id} value={pb.id}>
                    {`Checklist: ${pb.label}`}
                  </option>
                ))}
                <option value={PLAYBOOK_DECLINED}>No checklist</option>
              </select>
            ) : null}
          </div>
          <div className={styles.footTrail}>
            <button
              type="button"
              className={action.ghost}
              onClick={(e) => {
                setTemplatesTrigger(e.currentTarget);
                setTemplatesOpen(true);
              }}
              data-testid="new-mission-template"
            >
              <BookMarked size={13} aria-hidden="true" /> Template
            </button>
            <span className={styles.footSpacer} aria-hidden="true" />
            {/* The shortcut, for the eye. The textarea's `aria-keyshortcuts` is its accessible form. */}
            <span
              className={styles.footHint}
              aria-hidden="true"
              data-testid="new-mission-hint"
            >
              Ctrl + Enter
            </span>
            <button
              type="submit"
              className={action.primary}
              // A PROJECT IS REQUIRED. Without one the mission has no cwd, the server refuses
              // `running` for ever, and this console has no way to assign one afterwards — so
              // offering START would be offering a dead end (#896 review 9, finding 2).
              disabled={busy || !instruction.trim() || !projectId || overCap}
              data-testid="new-mission-start"
            >
              {busy ? "Starting…" : "Start mission"}
            </button>
          </div>
        </div>
        {templatesOpen ? (
          <TemplatePickerModal
            insertLabel="Insert into mission brief"
            onInsert={(t, values) => {
              // The same assembly the session composer pastes — body, then image paths — so a
              // template reads identically wherever it lands (#905's one-seam rule).
              const text = renderTemplate(t, values);
              setInstruction((prev) =>
                prev.trim() ? `${prev.replace(/\s+$/, "")}\n${text}` : text,
              );
              setTemplatesOpen(false);
            }}
            onClose={() => setTemplatesOpen(false)}
            returnFocusTo={templatesTrigger}
          />
        ) : null}
      </form>
      {/* WHAT THE CHOSEN CHECKLIST MEANS (#1061), under the box as the issue's mockup draws it: the
          playbook's own objective titles, or — for "No checklist" — its cost, in the text-safe amber,
          because a mission with nothing to check can never confirm itself finished. */}
      {playbooks.length ? (
        playbookId === PLAYBOOK_DECLINED ? (
          <div
            className={`${styles.playbookNote} ${styles.playbookNoteWarn} ${styles.boxNote}`}
            data-testid="new-mission-playbook-note"
          >
            No checklist — notes only: nothing can be checked, so the mission
            can never confirm itself finished. You close it.
          </div>
        ) : (
          <div
            className={`${styles.playbookNote} ${styles.boxNote}`}
            data-testid="new-mission-playbook-note"
          >
            {(() => {
              const pb = playbooks.find((p) => p.id === playbookId)!;
              const titles = pb.objectives.map((o) => o.title).filter(Boolean);
              return (
                <>
                  <strong>{pb.label}</strong>
                  {titles.length
                    ? ` — ${titles.join(" · ")}. Checked by the server; fitted to your instruction.`
                    : " — no objectives in this checklist yet."}
                </>
              );
            })()}
          </div>
        )
      ) : null}
      {/* Under the box: what the form needs from the operator, never inside the row. */}
      {note ? (
        <div
          id={noteId}
          className={
            noteVisible ? `${styles.objReason} ${styles.boxNote}` : "sr-only"
          }
          data-testid="new-mission-draft-note"
        >
          {note}
        </div>
      ) : null}
      {length > INSTRUCTION_MAX * 0.9 ? (
        <div
          className={`${overCap ? styles.objStale : styles.objReason} ${styles.boxNote}`}
          role="status"
          data-testid="new-mission-count"
        >
          {length} / {INSTRUCTION_MAX}
          {overCap ? " — shorten the brief to start this mission" : ""}
        </div>
      ) : null}
      {error ? (
        <div
          className={`${styles.objStale} ${styles.boxNote}`}
          role="alert"
          data-testid="new-mission-error"
        >
          {error}
        </div>
      ) : null}
    </>
  );
}
