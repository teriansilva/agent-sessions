/** The UNTRACKED view's Ask — `find` / `history` against `/api/pulse/ask` (#878).
 *
 * **These turns are transient because this view has no mission to keep them in**, and that is
 * now a statement about the surface rather than about unfinished work. A mission's turns ARE
 * durable: they go to `POST /api/missions/{id}/message` and live in the mission's timeline
 * (`MissionComposer`, #890). UNTRACKED is the sentinel view — a list of sessions no mission
 * owns — so there is no mission to own a turn either, and the honest answer is to say so on
 * screen rather than to invent a home for it. Creating a mission is how an operator makes a
 * conversation durable.
 *
 * **A reply may never land in a mission that did not ask for it.** The failure is easy to write
 * and invisible in review: select A, ask, switch to B before it resolves, and the late `then`
 * writes A's answer into B's thread. So every request carries the mission it was made for, and
 * any completion whose mission is no longer selected is DISCARDED rather than rendered — answer,
 * error and loading state alike. A spinner inherited by mission B is the same lie in a quieter
 * form.
 *
 * The mechanism has two halves, and both are needed.
 *
 * **Keying** — the console mounts this inside a subtree keyed on the mission, so a switch
 * unmounts it. The draft text and the busy flag go with it, and mission B cannot inherit A's
 * spinner because B's composer is a different instance.
 *
 * **A live-ness fence** — the in-flight request keeps running (`api.pulseAsk` takes no abort
 * signal, so an `AbortController` here would abort nothing while reading as though it did), and
 * its callbacks close over the mission they were made for. `#878` requires a completion whose
 * mission is no longer selected to be **discarded, not filed**: an answer written into a mission
 * the operator has left is state they never asked to keep and will meet later with no context
 * for it. So the callbacks ask the console whether that mission is still the selected one, and
 * drop the result when it is not.
 *
 * An earlier revision kept the answer under A on the reasoning that A's own answer should be
 * waiting when the operator returns. That is a UX preference, and it loses to the stated
 * contract: the safety property is satisfied either way, and where they differ the contract
 * decides. `Composer.test.tsx` asserts NEITHER mission receives the stale completion.
 *
 * **NEW MISSION is a MODE of this component, not a modal (#889).** The page already owns exactly
 * one focus trap — the rail drawer, whose contract #878 pinned — and a second would make it two
 * on desktop and a third surface on mobile. Putting the mode here also makes it reachable in
 * every state the console can be in, including a completely empty install: the composer renders
 * under UNTRACKED and under the no-missions-and-no-sessions empty state as well as inside a
 * mission, so there is never a screen from which a first mission cannot be started.
 *
 * **The project is an ENTITY id, never a folder cwd.** `POST /api/missions` resolves the working
 * directory server-side from `project_id` against the project store, and rejects a client-sent
 * `cwd` outright (422). A folder row's id IS its cwd, so sending one here is a 404 "unknown
 * project" — the two pickers are not interchangeable, which is why this reads
 * `api.projectEntities()` and not `api.folders()`.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { Link } from "react-router-dom";

import { api, ApiError } from "../../lib/api";
import type { Mission, ProjectEntity, PulseAskMatch } from "../../types/api";

import styles from "./mission.module.css";

export interface AskTurn {
  id: number;
  question: string;
  answer: string | null;
  error: string | null;
  /** The sessions the answer is ABOUT, each with the reason it matched. Without these an answer
   *  naming a session gives the operator no way to reach it — the Ask box rendered them and the
   *  console must too. */
  matches: PulseAskMatch[];
}

let nextTurnId = 1;

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
function matchRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** The NEW MISSION half of the composer. Split out so the ask path keeps its own state and the
 *  two modes cannot leak into each other — a half-typed instruction must not become a question.
 *
 *  The project list loads lazily, once the operator opens the mode: an install with no projects
 *  is a real state and says so, rather than offering an empty `<select>` that silently posts
 *  nothing. The project is REQUIRED here — START stays disabled without one. The server will
 *  take a project-less create and make a `draft`, but a mission that cannot run until somebody
 *  notices is not what an operator pressing START asked for, and the one moment they are looking
 *  at a picker is the cheapest moment to answer it. */
function NewMissionForm({
  onCreated,
  onCancel,
  visit,
  isVisitCurrent,
}: {
  /** `focus` says whether the console should SELECT the new mission.
   *
   *  A create that resolves after the operator cancelled or moved on still has to refresh the
   *  rail — the mission exists, and hiding it would be worse than showing it — but it must not
   *  steal the selection. Those are two different things and were one before (#896 review 2). */
  onCreated: (m: Mission, opts: { focus: boolean }) => void;
  onCancel: () => void;
  /** The visit this create belongs to, captured when START is pressed. */
  visit: () => number;
  /** Is that visit still the one on screen? Read at RESOLUTION time, never captured. */
  isVisitCurrent: (at: number) => boolean;
}) {
  const [instruction, setInstruction] = useState("");
  const [projectId, setProjectId] = useState("");
  const [projects, setProjects] = useState<ProjectEntity[] | null>(null);
  /** The list could not be READ — a different fact from an empty one, and with a different fix. */
  const [projectsError, setProjectsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** False once this form has been cancelled or unmounted. A create is not abortable, so the
   *  completion has to be fenced rather than the request cancelled — the same shape as the ask
   *  path's liveness fence above, and for the same reason. */
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
      if (!text || busy || !projectId) return;
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
        });
        // FENCED AT RESOLUTION, ON THE VISIT. The operator may have cancelled, or moved to
        // another mission, or flipped Active → Archived while this was in flight. The last of
        // those is the case an id fence cannot see: this form sits in the UNTRACKED view, which
        // the scope flip does not unmount and whose sentinel id is unchanged, so "the same view
        // is showing" stayed true while the rail underneath it became a different set (#896
        // review 10, finding 4).
        //
        // The mission still EXISTS, so the rail is refreshed either way; only the focus is
        // withheld. Dropping it entirely would leave a real mission invisible until the next poll.
        const focus = liveRef.current && isVisitCurrent(at);
        setInstruction("");
        setProjectId("");
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
    [instruction, projectId, busy, onCreated, visit, isVisitCurrent],
  );

  return (
    <form
      className={styles.newMissionForm}
      onSubmit={submit}
      data-testid="new-mission-form"
      aria-label="Start a new mission"
    >
      <div className={styles.newMissionRow}>
        <textarea
          className={styles.composerInput}
          rows={1}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="What should this mission achieve?"
          aria-label="Mission instruction"
          data-testid="new-mission-instruction"
        />
        <select
          className={styles.newMissionProject}
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          aria-label="Project"
          data-testid="new-mission-project"
        >
          <option value="">
            {projects === null
              ? "Loading projects…"
              : projectsError
                ? "The project list could not be read"
                : projects.length === 0
                  ? "No projects yet"
                  : "Choose a project…"}
          </option>
          {(projects ?? []).map((pr) => (
            <option key={pr.id} value={pr.id}>
              {pr.name}
            </option>
          ))}
        </select>
      </div>
      {/* SAID BEFORE THE CREATE, and it names the fix rather than a control that does not exist.
          The earlier copy said "pick one before it can run", which was a promise this console
          cannot keep: a mission created without a project has no cwd, the server then refuses
          `running` for ever, and there is no assignment path to reach afterwards (#896 review 9,
          finding 2). So a project is REQUIRED where one can be offered, and where none can be —
          an empty list, or one that would not load — the message says which and what to do. */}
      {projects !== null && !projectId ? (
        <div className={styles.objReason} data-testid="new-mission-draft-note">
          {projectsError
            ? "The project list could not be read, so there is nothing to start this in. Try again in a moment."
            : projects.length === 0
              ? "No projects yet. Add one in Settings → Projects, then start the mission here."
              : "Pick the project this mission works in. It decides where its agent runs."}
        </div>
      ) : null}
      {error ? (
        <div
          className={styles.objStale}
          role="alert"
          data-testid="new-mission-error"
        >
          {error}
        </div>
      ) : null}
      <div className={styles.newMissionActions}>
        <button
          type="button"
          className={styles.missionBtn}
          onClick={() => {
            // Cancelling while a create is in flight does not un-send it — the mission will
            // exist. It withdraws the FOCUS: the operator said they were done here.
            liveRef.current = false;
            onCancel();
          }}
          data-testid="new-mission-cancel"
        >
          CANCEL
        </button>
        <button
          type="submit"
          className={styles.send}
          // A PROJECT IS REQUIRED. Without one the mission has no cwd, the server refuses
          // `running` for ever, and this console has no way to assign one afterwards — so
          // offering START would be offering a dead end (#896 review 9, finding 2).
          disabled={busy || !instruction.trim() || !projectId}
          data-testid="new-mission-start"
        >
          {busy ? "…" : "START"}
        </button>
      </div>
    </form>
  );
}

export function Composer({
  missionId,
  configured,
  onTurns,
  turns,
  visit,
  isVisitCurrent,
  onCreated,
  creating,
  onCreatingChange,
}: {
  /** The mission these turns belong to. Ownership is keyed on it. */
  missionId: string;
  /** False when no AI endpoint is configured. `/api/pulse/ask` answers 409 in that case and has
   *  no local fallback, so the control is disabled and says why — `find` / `history` genuinely
   *  do not work without a model. */
  configured: boolean;
  /** WHICH MODE, owned by the console (#935; reshaped in #937 review 1).
   *
   *  This began as a nonce — "start creating now" — so the mode could stay local. Two rounds
   *  later that was wrong twice over: the request had to survive a MOUNT (the rail's button
   *  switches branches, which remounts this composer), and consuming it meant `setState` inside
   *  an effect, which is a cascading render the react-hooks rule rejects. Both problems are the
   *  same problem — the mode outlives this component, so this component should not own it.
   *
   *  Only the MODE is lifted. The draft text, the pending send and the turn history stay here,
   *  which is what #930's remount fixes were about. */
  creating: boolean;
  onCreatingChange: (creating: boolean) => void;
  turns: AskTurn[];
  onTurns: (missionId: string, fn: (prev: AskTurn[]) => AskTurn[]) => void;
  /** The visit a request is started in, and whether it is still on screen. Captured at SEND
   *  time; an id can be true again, a visit cannot.
   *
   *  This replaced an `isCurrent(missionId)` fence (#896 review 11, finding 2). I argued for the
   *  id one round earlier — that the turns live in the console "so switching away and back does
   *  not discard an answer", so a round trip should keep the reply — and that was wrong about
   *  which contract exists. The contract is DISCARD ON NAVIGATION: moving away already deletes
   *  the pending turn, and `Composer.test.tsx` has asserted exactly that since #878. So the id
   *  fence did not preserve an answer, it preserved one arbitrary subset of them — the ones
   *  whose operator happened to come back before the reply landed — and admitted a result from a
   *  visit that was over into a visit that is not. */
  visit: () => number;
  isVisitCurrent: (at: number) => boolean;
  /** A mission was just created here. `focus` is false when the completion arrived after the
   *  operator cancelled or moved on: the rail still refreshes, the selection does not move. */
  onCreated: (m: Mission, opts: { focus: boolean }) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  /** Focus the field the operator just asked for. Not `setState`, so no cascading render — and
   *  the field does not exist until the create branch has rendered, hence the frame wait. */
  useEffect(() => {
    if (!creating) return;
    const raf = requestAnimationFrame(() => {
      document
        .querySelector<HTMLTextAreaElement>(
          '[data-testid="new-mission-instruction"]',
        )
        ?.focus();
    });
    return () => cancelAnimationFrame(raf);
  }, [creating]);
  /** The mission currently owning an in-flight request, read inside the late callback. A ref,
   *  not state, because the callback must see the value at RESOLUTION time, not the one captured
   *  when the request started. */

  const submit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const q = text.trim();
      if (!q || busy || !configured) return;
      const asked = missionId; // the mission this turn belongs to, captured once
      const at = visit(); // …and the VISIT to it, which the id alone cannot distinguish
      const id = nextTurnId++;
      onTurns(asked, (prev) => [
        ...prev,
        { id, question: q, answer: null, error: null, matches: [] },
      ]);
      setText("");
      setBusy(true);
      try {
        // Prior turns of THIS mission only — the history is per-mission for the same reason the
        // answers are.
        const history = turns.flatMap((t) =>
          t.answer !== null
            ? [
                { role: "user" as const, content: t.question },
                { role: "assistant" as const, content: t.answer },
              ]
            : [],
        );
        const r = await api.pulseAsk(q, history);
        // The operator moved on: discard, and take the pending turn with it so no half-finished
        // row is left behind either.
        if (!isVisitCurrent(at)) {
          onTurns(asked, (prev) => prev.filter((t) => t.id !== id));
          return;
        }
        onTurns(asked, (prev) =>
          prev.map((t) =>
            t.id === id
              ? { ...t, answer: r.answer ?? "", matches: r.matches ?? [] }
              : t,
          ),
        );
      } catch (err) {
        if (!isVisitCurrent(at)) {
          onTurns(asked, (prev) => prev.filter((t) => t.id !== id));
          return;
        }
        const msg = err instanceof Error ? err.message : "That didn't work.";
        onTurns(asked, (prev) =>
          prev.map((t) => (t.id === id ? { ...t, error: msg } : t)),
        );
      } finally {
        // Safe unconditionally: on a mission switch this instance is already unmounted, so the
        // call is a no-op rather than a write into another mission's composer.
        setBusy(false);
      }
    },
    [text, busy, configured, missionId, onTurns, turns, visit, isVisitCurrent],
  );

  return (
    <>
      {turns.length > 0 ? (
        <div data-testid="ask-turns">
          {turns.map((t) => (
            <div key={t.id} className={styles.event} data-testid="ask-turn">
              <div className={styles.eventHead}>You</div>
              <div className={styles.eventText}>{t.question}</div>
              {t.answer !== null ? (
                <>
                  <div className={styles.eventHead} style={{ marginTop: 6 }}>
                    Answer
                  </div>
                  <div className={styles.eventText}>{t.answer}</div>
                  {/* The matched sessions, each with why it matched and a way in. An answer that
                      names a session the operator cannot reach is half an answer. */}
                  {t.matches.map((m) => (
                    <div
                      key={m.id}
                      className={styles.matchRow}
                      data-testid="ask-match"
                    >
                      <div className={styles.eventText}>{m.title}</div>
                      {m.why ? (
                        <div className={styles.objReason}>{m.why}</div>
                      ) : null}
                      <Link
                        className={styles.openSession}
                        to={matchRoute(m.id)}
                        aria-label={`Jump into ${m.title}`}
                      >
                        Jump in
                      </Link>
                    </div>
                  ))}
                </>
              ) : t.error ? (
                <div className={styles.objStale} data-testid="ask-error">
                  {t.error}
                </div>
              ) : (
                <div className={styles.objReason}>…</div>
              )}
            </div>
          ))}
          <div className={styles.objReason} data-testid="ask-transient">
            These answers are not kept — this view has no mission to keep them
            in, so they disappear when you reload. A mission's own conversation
            is saved.
          </div>
        </div>
      ) : null}

      {/* The mode strip. NEW MISSION is available even with no AI endpoint — creating, adopting
          and objectives all work without a model, and only the ASK half genuinely needs one. */}
      <div
        className={styles.composerModes}
        role="group"
        aria-label="Composer mode"
      >
        <button
          type="button"
          className={`${styles.modeBtn} ${creating ? "" : styles.modeOn}`}
          aria-pressed={!creating}
          onClick={() => onCreatingChange(false)}
          data-testid="composer-mode-ask"
        >
          ASK
        </button>
        <button
          type="button"
          className={`${styles.modeBtn} ${creating ? styles.modeOn : ""}`}
          aria-pressed={creating}
          onClick={() => onCreatingChange(true)}
          data-testid="composer-mode-new"
        >
          NEW MISSION
        </button>
      </div>

      {creating ? (
        <NewMissionForm
          visit={visit}
          isVisitCurrent={isVisitCurrent}
          onCreated={(m, opts) => {
            // ONLY THE CREATION STILL ON SCREEN MAY CLOSE THE FORM (#937 review 2).
            //
            // Lifting the mode to the console widened this setter's lifetime, and that turned a
            // harmless no-op into data loss. Before: a slow create A settled after its composer
            // had unmounted, and `setCreating(false)` wrote to a dead component. After: the same
            // late response reaches the console's SHARED mode and closes whatever form is open
            // now — so an operator who started A, went to another mission, came back and typed
            // draft B lost B the moment A's response landed.
            //
            // `focus` is the fence `NewMissionForm` already computes for exactly this question
            // (`liveRef.current && isVisitCurrent(at)`), so the mode reuses that decision rather
            // than inventing a second, differently-wrong one. A stale creation still refreshes
            // the rail — the mission is real and must appear — it just does not touch the mode.
            if (opts?.focus !== false) onCreatingChange(false);
            onCreated(m, opts);
          }}
          onCancel={() => onCreatingChange(false)}
        />
      ) : (
        <form className={styles.composer} onSubmit={submit}>
          <textarea
            className={styles.composerInput}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={!configured}
            placeholder={
              configured
                ? "Ask about your work — find, history…"
                : "Needs an AI endpoint"
            }
            aria-label="Ask about your past work"
            data-testid="composer-input"
          />
          <button
            type="submit"
            className={styles.send}
            disabled={!configured || busy || !text.trim()}
            data-testid="composer-send"
          >
            {busy ? "…" : "SEND"}
          </button>
        </form>
      )}
    </>
  );
}
