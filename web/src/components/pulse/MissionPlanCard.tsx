/** The proposal, and the button that runs it (#893 Phase 4).
 *
 * **The separation IS the feature.** `/plan` spends a model call and writes a row; nothing starts.
 * The operator reads which project, which agent and what the agent will be told, changes any of
 * it, and then presses a different button. A card that planned and dispatched in one tap would be
 * a button whose consequences the operator only learns about afterwards — and those consequences
 * are an agent running UNATTENDED in a real working directory. Not permission-bypassed — see
 * `routes/missions` — which means it can also stop on a tool prompt with nobody there to answer.
 *
 * **Every edit names the plan it edited.** The route replaces the whole row, so two tabs editing
 * different fields of one proposal would both be told the save worked while the later write
 * restored its own stale copy of the other's field. `plan_id` is the comparand; a 409 means the
 * plan moved and the card re-reads rather than retrying (#904 review 6).
 *
 * **DISPATCH confirms.** It is the highest-privilege control in the app and it takes the same two
 * taps ARCHIVE and MARK DONE take, with the confirmation saying what it does — starts an agent,
 * unattended, in this directory.
 *
 * **WHAT DONE MEANS IS PART OF THE DECISION** (#904 review 2, finding 8). #893 asks for the
 * objectives on this card before DISPATCH acts, and the reason is a mobile one: on a phone the
 * objectives are a separate stop, so an operator could start an unattended agent without ever
 * seeing — or noticing the absence of — the checklist the supervisor will follow through on.
 *
 * They are shown here, and a mission with NONE says so rather than showing an empty space that
 * reads as "nothing required". EDITING stays in the objectives pane: it is a real editor with
 * probes, gates, ordering and its own validation, and a second copy of it inside a decision card
 * is two implementations of one contract. What this card owes the operator is that they cannot
 * approve a launch without having been told what completion means; a link is enough for the rest,
 * and duplicating the editor is tracked separately rather than smuggled in here.
 *
 * **The project picker sends an ENTITY ID, never a path.** The server resolves the working
 * directory from the entity, at the moment of dispatch — so a project repointed while this card
 * sat on screen launches where it points NOW, not where it pointed when the plan was written.
 * There is no code path from anything typed here to a launch argument.
 *
 * **The card is KEYED on `plan_id` by the console**, which is what makes the brief draft and the
 * confirmation belong to one proposal rather than being reset by an effect. A re-plan or another
 * tab's edit mints a new id, so it is a new card; a poll that returns the same proposal is the
 * same card and does not fight the operator's typing.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { objectivesDigestInput, sha256Hex } from "../../lib/digest";
import type { Mission } from "../../types/api";

import styles from "./mission.module.css";

/** The states a plan may be produced for. Mirrors the server's `PLANNABLE_STATES`, and the card
 *  offers PLAN only from them — a proposal for a mission that is already running is a proposal to
 *  start it twice. */
const PLANNABLE = new Set(["draft", "planned"]);

function why(dropped: string[] | undefined, field: string): string | null {
  return (dropped ?? []).includes(field) ? "the model did not give one" : null;
}

export function MissionPlanCard({
  mission,
  onChanged,
  onNote,
}: {
  mission: Mission;
  /** The mission row changed — the plan rides on it, and so does the state a plan establishes. */
  /** The mission changed on the server. `membershipChanged` says the change touched which
   *  SESSIONS it holds — true for a dispatch, which attaches one — so the console can refresh the
   *  overview as well as the rail (#904 review 18, finding 2). */
  onChanged: (opts?: { membershipChanged?: boolean }) => void;
  onNote: (msg: string) => void;
}) {
  const plan = mission.plan ?? null;
  const state = mission.state;
  const [busy, setBusy] = useState<string | null>(null);
  /** THE CHECKLIST THE FIRST TAP APPROVED, not merely "a confirmation is armed" (#904 review 9,
   *  finding 3). `confirming` used to be a boolean while the digest was recomputed from live
   *  props, so a poll landing between the two taps left the button reading CONFIRM DISPATCH and
   *  sent the NEW checklist's digest — the server's compare-and-set then passed for a set the
   *  operator had never confirmed. Holding the digest is what makes "the same one you read" mean
   *  the same thing on both taps. */
  const [confirming, setConfirming] = useState<string | null>(null);
  const [brief, setBrief] = useState(plan?.brief ?? "");
  const [error, setError] = useState<string | null>(null);
  /** The operator's own objective, for the empty-checklist recovery (#904 review 7, finding 3).
   *
   *  UP HERE with its siblings, not beside the handler that uses it: everything below the
   *  `PLANNABLE` early return is conditional, and a hook called after a return is called in a
   *  different order on the renders that take it — which is what `rules-of-hooks` exists to
   *  catch, and did. */
  const [objTitle, setObjTitle] = useState("");

  const objectives = useMemo(
    () => mission.objectives ?? [],
    [mission.objectives],
  );
  const objectivesPending = mission.objectives_state === "pending";
  /** THE CHECKLIST THIS CARD IS SHOWING, digested exactly as the server digests it (#904 review
   *  3, finding 5). Sent with DISPATCH so "final" and "the same set you read" are one check —
   *  the mission starts with `objectives_state: "pending"` and a fast operator could otherwise
   *  launch before it knew what finishing means. Key, title and GATE only: an objective becoming
   *  met between the card and the button is progress, not a different checklist. */
  const objectivesDigest = useMemo(
    () => sha256Hex(objectivesDigestInput(objectives)),
    [objectives],
  );

  const fail = useCallback((err: unknown, fallback: string) => {
    setError(err instanceof ApiError && err.message ? err.message : fallback);
  }, []);

  const propose = useCallback(async () => {
    if (busy) return;
    setBusy("plan");
    setError(null);
    try {
      await api.planMission(mission.id);
      onChanged();
    } catch (err) {
      fail(err, "That plan could not be produced.");
    } finally {
      setBusy(null);
    }
  }, [busy, mission.id, onChanged, fail]);

  const edit = useCallback(
    async (body: {
      project_id?: string | null;
      engine?: string;
      brief?: string;
    }) => {
      if (busy || !plan) return;
      setBusy("edit");
      setError(null);
      try {
        await api.editMissionPlan(mission.id, plan.plan_id, body);
      } catch (err) {
        // A 409 means the plan moved underneath this card. Re-reading is the whole remedy — a
        // retry would write the stale fields this card is still holding.
        fail(err, "That change could not be saved.");
      } finally {
        setBusy(null);
        // ALWAYS, success or failure: on a 409 especially, because the point is that what is on
        // screen is not what the server holds.
        onChanged();
      }
    },
    [busy, plan, mission.id, onChanged, fail],
  );

  /** …AND THE ARM IS RELEASED WHEN THE APPROVED SET CHANGES. Sending the armed digest alone is
   *  safe — the server refuses it — but a 409 is a worse answer than not offering the tap: the
   *  operator's next move is to read the list again, so the card says so instead of letting them
   *  confirm something that cannot land. */
  useEffect(() => {
    if (confirming === null) return;
    let live = true;
    void objectivesDigest.then((now) => {
      if (!live || now === confirming) return;
      setConfirming(null);
      onNote(
        "The checklist changed while you were confirming — read it again.",
      );
    });
    return () => {
      live = false;
    };
  }, [confirming, objectivesDigest, onNote]);

  const dispatch = useCallback(async () => {
    if (busy || !plan || confirming === null) return;
    setBusy("dispatch");
    setError(null);
    try {
      // THE PATH THE OPERATOR APPROVED, sent as a COMPARAND (#904 review 2, finding 6). The
      // server resolves the project again at the write boundary — it must, because a project can
      // be repointed while a proposal sits on screen — and without this it would then launch a
      // directory the confirmation never named. The client cannot CHOOSE a path (the route
      // refuses one), and this is not one: it is an assertion about which resolution was shown,
      // and a mismatch is a 409 that says to read the plan again.
      const out = await api.dispatchMission(
        mission.id,
        plan.plan_id,
        plan.cwd ?? "",
        // THE ARMED DIGEST, never a fresh read of the props. This is the whole point of holding
        // it: the comparand has to name what the confirmation was about.
        confirming,
      );
      // The SERVER'S own verdict, not "started". A dispatch that reached a live process and no
      // agent is a failure with a reason, and the reason is the useful half.
      if (out.state !== "running") {
        onNote(out.reason || `The dispatch ended as ${out.state}.`);
      }
    } catch (err) {
      fail(err, "That dispatch could not be started.");
    } finally {
      setBusy(null);
      setConfirming(null);
      // A DISPATCH ATTACHES A SESSION, so this one moved membership: the console refreshes the
      // overview too, and the newly owned session leaves UNTRACKED at once rather than at the
      // next supervisor sweep two and a half minutes later.
      onChanged({ membershipChanged: true });
    }
  }, [busy, plan, confirming, mission.id, onChanged, onNote, fail]);

  // NOTHING AT ALL ONCE THE MISSION HAS LEFT THE PLANNING STATES (#904 review 17, non-blocking).
  // This gate lived INSIDE the `!plan` branch, so it only ever covered a mission with no
  // proposal. A mission that still carries a RETAINED plan — the ordinary case after a dispatch,
  // since the row outlives the states that may act on it — fell straight through to the full
  // card, offering EDIT and DISPATCH that the server answers with a 409. The one existing test
  // mounted `running` with no plan, so it passed without touching the case.
  //
  // After every hook, and before anything renders: a return above the hooks would call them in a
  // different order on the next pass, which is the rule the comment at the top of this file is
  // about.
  if (!PLANNABLE.has(state)) return null;
  if (!plan) {
    return (
      <div className={styles.planCard} data-testid="mission-plan-card">
        <div className={styles.planLead}>
          No plan yet. Work out which project, which agent, and what to tell it.
        </div>
        <button
          type="button"
          className={styles.send}
          disabled={busy !== null}
          onClick={() => void propose()}
          // NOT `mission-plan` (#904 review 16, found by the merge). `MissionLifecycle`'s READY
          // button owns that id — it moves the mission INTO `planned` — and both branches chose
          // it independently, so merging them put two on the page and every strict-mode locator
          // matched both. This one ASKS FOR A PROPOSAL, which is a different act, and it now
          // reads like the rest of the card's `mission-plan-*` family.
          data-testid="mission-plan-propose"
        >
          {busy === "plan" ? "…" : "PLAN THIS"}
        </button>
        {error ? (
          <div
            className={styles.planError}
            // ANNOUNCED. These arrive from an await — a refused plan, a 409 on dispatch — so
            // nothing moves focus and a silent region is a failure the operator never hears.
            role="alert"
            data-testid="mission-plan-error"
          >
            {error}
          </div>
        ) : null}
      </div>
    );
  }

  const projects = plan.project_options ?? [];
  const engines = plan.engine_options ?? [];
  // WHAT IS ON SCREEN MUST BE WHAT WOULD RUN (#904 review 2, finding 5).
  //
  // `ready` was computed from the PERSISTED brief while the textarea rendered the local draft.
  // Clearing the textarea skips the blur save — an empty brief is not a save, it is a deletion
  // the server would refuse — so the button stayed enabled on the old persisted value, and two
  // taps then ran text the operator believed they had removed.
  //
  // So the draft has to MATCH what is stored. An unsaved edit is not "not ready yet" in some
  // vague sense; it is a different brief from the one DISPATCH would send, and the card says so.
  const addObjective = async () => {
    const title = objTitle.trim();
    if (!title || busy !== null) return;
    setBusy("objective");
    setError(null);
    try {
      // The KEY is derived server-side from nothing — it has to be supplied — so it is built from
      // the title the operator typed rather than asked for separately: one field, not two.
      const key =
        title
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "_")
          .replace(/^_+|_+$/g, "")
          .slice(0, 40) || `objective_${Date.now()}`;
      await api.patchMissionObjectives(mission.id, [
        { op: "add", key, title, gate: true },
      ]);
      setObjTitle("");
      onChanged();
    } catch (e) {
      setError(
        e instanceof ApiError && e.message
          ? e.message
          : "That objective could not be added.",
      );
    } finally {
      setBusy(null);
    }
  };

  const draft = brief.trim();
  const unsaved = draft !== (plan.brief ?? "");
  const ready =
    Boolean(plan.project_id && plan.engine && plan.brief) &&
    !unsaved &&
    // NOT WHILE THE CHECKLIST IS STILL ARRIVING. A dispatch that beats the objective producer
    // starts a mission that does not yet know what finishing means, and the supervisor then
    // follows through against a set that landed afterwards.
    !objectivesPending &&
    // …AND NOT WITH AN EMPTY ONE (#904 review 4, finding 3). The card used to enable the launch
    // beside a paragraph explaining that the agent would "run with nothing to check it against"
    // — a warning where #893's acceptance invariant needs a gate. The server refuses this too;
    // the card refusing as well is what stops the operator taking two taps to learn it.
    objectives.length > 0;

  return (
    <div className={styles.planCard} data-testid="mission-plan-card">
      <div className={styles.planLead}>The plan</div>

      <label className={styles.planLabel} htmlFor="plan-project">
        Project
      </label>
      <select
        id="plan-project"
        className={styles.planField}
        value={plan.project_id ?? ""}
        disabled={busy !== null}
        onChange={(e) => void edit({ project_id: e.target.value || null })}
        data-testid="mission-plan-project"
      >
        <option value="">
          {projects.length ? "Choose a project…" : "No projects configured"}
        </option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {!plan.project_id ? (
        <div className={styles.planWhy} data-testid="mission-plan-no-project">
          {why(plan.dropped, "project") ??
            "Pick one — a mission with no project has nowhere to run."}
        </div>
      ) : null}

      <label className={styles.planLabel} htmlFor="plan-engine">
        Agent
      </label>
      <select
        id="plan-engine"
        className={styles.planField}
        value={plan.engine ?? ""}
        disabled={busy !== null}
        onChange={(e) => void edit({ engine: e.target.value })}
        data-testid="mission-plan-engine"
      >
        <option value="">
          {engines.length ? "Choose an agent…" : "No agent can be dispatched"}
        </option>
        {engines.map((e) => (
          <option key={e.id} value={e.id}>
            {e.label}
          </option>
        ))}
      </select>
      {/* WHY THIS AGENT, in the model's words. A suggestion with no stated reason is a decision
          disguised as a default, which is the thing this card refuses to make. */}
      <div className={styles.planWhy} data-testid="mission-plan-reason">
        {plan.engine
          ? plan.engine_reason || "you chose this"
          : (why(plan.dropped, "engine") ?? "Pick the agent to run this.")}
      </div>

      <label className={styles.planLabel} htmlFor="plan-brief">
        What the agent is told
      </label>
      <textarea
        id="plan-brief"
        className={styles.planBrief}
        rows={4}
        value={brief}
        disabled={busy !== null}
        onChange={(e) => setBrief(e.target.value)}
        onBlur={() => {
          const next = brief.trim();
          if (next && next !== plan.brief) void edit({ brief: next });
        }}
        data-testid="mission-plan-brief"
      />

      {/* WHAT DONE MEANS. Shown here because an operator approving an unattended launch has to
          know what the supervisor will chase — and, when there is none, WRITABLE here (#904
          review 7, finding 3).

          The refusal used to point at the OBJECTIVES stop, which is a read-only list in this PR.
          So an install whose objective production is `skipped` (no AI endpoint configured) or
          `failed` could never dispatch at all: the card refused, and the place it named could not
          fix it. The recovery path has to exist where the refusal is. */}
      <div className={styles.planLabel}>What done means</div>
      {objectives.length === 0 ? (
        <div data-testid="mission-plan-no-objectives">
          <div className={styles.planWhy}>
            {mission.objectives_state === "pending"
              ? "Still working out what done means for this mission…"
              : "No objectives yet — so DISPATCH is off. Add at least one below; the supervisor has nothing to follow through on without it."}
          </div>
          {mission.objectives_state === "pending" ? null : (
            <div className={styles.planAddObjective}>
              <input
                className={styles.planField}
                value={objTitle}
                placeholder="e.g. A PR is open and its checks are green"
                aria-label="What done means"
                disabled={busy !== null}
                onChange={(e) => setObjTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void addObjective();
                }}
                data-testid="plan-objective-title"
              />
              <button
                type="button"
                className={styles.planBtn}
                disabled={busy !== null || !objTitle.trim()}
                onClick={() => void addObjective()}
                data-testid="plan-objective-add"
              >
                {busy === "objective" ? "…" : "ADD"}
              </button>
            </div>
          )}
        </div>
      ) : (
        <ul
          className={styles.planObjectives}
          data-testid="mission-plan-objectives"
        >
          {objectives.map((o) => (
            <li key={o.key}>
              <span className={styles.planObjGate}>
                {o.gate ? "gate" : "goal"}
              </span>{" "}
              {o.title || o.key}
            </li>
          ))}
        </ul>
      )}

      {unsaved ? (
        <div className={styles.planWhy} data-testid="mission-plan-unsaved">
          {draft
            ? "This brief has not been saved yet — tap outside the box to save it."
            : "A brief is required. Type one, or RE-PLAN to get a new proposal."}
        </div>
      ) : null}

      {/* DISPATCH CONFIRMS. It starts an agent with nobody watching it, in a real directory, and
          the confirmation says exactly that rather than asking "are you sure?". */}
      {confirming !== null ? (
        <div
          className={styles.planConfirm}
          // NAMED, AND ANNOUNCED (#904 review 18, a11y). This sentence is the whole confirmation
          // — an unattended agent, in a named directory — and the button beside it only says
          // "CONFIRM DISPATCH". Unassociated, a screen reader reaches the button having never
          // read the consequence. `id` ties it to the button via `aria-describedby`; the live
          // region is because it APPEARS on the first tap rather than being there to find.
          id="mission-dispatch-consequence"
          role="status"
          aria-live="polite"
          data-testid="mission-dispatch-confirm"
        >
          This starts {plan.engine} in {plan.cwd}, unattended.
        </div>
      ) : null}
      <button
        type="button"
        className={styles.send}
        disabled={busy !== null || !ready}
        aria-describedby={
          confirming !== null ? "mission-dispatch-consequence" : undefined
        }
        onClick={() =>
          confirming !== null
            ? void dispatch()
            : void objectivesDigest.then(setConfirming)
        }
        title={
          ready
            ? "Start this agent now"
            : unsaved
              ? "Save the brief first — DISPATCH runs what is stored, not what is typed"
              : objectivesPending
                ? "Wait for the objectives — a mission that does not know what done means has nothing to follow through on"
                : objectives.length === 0
                  ? "Add what done means first — an unattended agent with no objectives has nothing to follow through on"
                  : "A plan needs a project, an agent and a brief before it can run"
        }
        data-testid="mission-dispatch"
      >
        {busy === "dispatch"
          ? "…"
          : confirming !== null
            ? "CONFIRM DISPATCH"
            : "DISPATCH"}
      </button>
      <button
        type="button"
        className={styles.planBtn}
        disabled={busy !== null}
        onClick={() => void propose()}
        data-testid="mission-replan"
      >
        {busy === "plan" ? "…" : "RE-PLAN"}
      </button>
      {error ? (
        <div
          className={styles.planError}
          // ANNOUNCED. These arrive from an await — a refused plan, a 409 on dispatch — so
          // nothing moves focus and a silent region is a failure the operator never hears.
          role="alert"
          data-testid="mission-plan-error"
        >
          {error}
        </div>
      ) : null}
    </div>
  );
}
