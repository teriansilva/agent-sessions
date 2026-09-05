/** MISSION CONTROL — the console (#878, Phase 2b of #840).
 *
 * A rail of missions, a thread per mission, and a detail column stacking objectives, context and
 * the timeline. This replaces the card grid: a session that used to be a card is now a mission
 * row, and a live session no mission owns appears under UNTRACKED with ADOPT — which is both the
 * migration path and the permanent home for work started from the sidebar.
 *
 * Two breakpoints, THREE modes. The middle one (1100–1399: rail is a column, detail is still a
 * tab strip) is the mode neither Playwright project lands on, so the breakpoint spec tests both
 * edges of both boundaries rather than a representative width.
 *
 * Every decision renders through the SERVER's projection (`projection` / `can_approve` /
 * `can_reject`). Nothing here re-derives controls from `state` — that drift is what #862 exists
 * to end, and `ActionRow` already consumes the contract.
 *
 * **Per-mission state is fenced by REMOUNTING, not by resetting.** `MissionBody` is keyed on the
 * mission id, so switching missions destroys the old instance rather than clearing it field by
 * field. A late response from the previous mission then resolves into an unmounted component and
 * updates nothing — a guarantee that cannot be forgotten when a sixth fetch is added, unlike a
 * captured-id check inside every `.then`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { actionOutcome } from "../../lib/orchestratorAction";
import type {
  Mission,
  MissionEvent,
  MissionListRow,
  OrchestratorAction,
  PulseAskMatch,
  PulseCard,
} from "../../types/api";

import { Link } from "react-router-dom";

import { HudFrame } from "../hud/HudFrame";
import { ActionRow } from "./ActionRow";
import { Composer, type AskTurn } from "./Composer";
import { MissionComposer } from "./MissionComposer";
import { MissionQuestionCard } from "./MissionQuestionCard";
import { MissionDrawer } from "./MissionDrawer";
import { MissionPlanCard } from "./MissionPlanCard";
import { MissionLifecycle } from "./MissionLifecycle";
import { type ObjectiveOp } from "./MissionObjectives";
import { MissionRail, UNTRACKED_VIEW } from "./MissionRail";
import { BAND_LABEL } from "./bands";
import { ObjectivesPane, TimelinePane } from "./MissionDetail";
import { useMissionDetail } from "./useMissionDetail";
import styles from "./mission.module.css";

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** Does the action have prose of its own? (#781.)
 *
 *  When it does, the review's ⚠ marker AND its summary both stand down — three descriptions of
 *  one session was the defect. The test is on the action HAVING PROSE, never on it existing:
 *  `rationale` may legitimately be empty (`str(item.get("rationale") or "")`) and `ActionRow`
 *  renders no line for an empty one, so keying on existence alone would leave a block carrying
 *  controls and no explanation at all. */
function actionSpeaks(c: PulseCard): boolean {
  return !!c.pending_action?.rationale?.trim();
}

/** One page of the rail. The server caps `limit` itself; this is the client's step size. */
const PAGE = 100;
/** The server's own page ceiling (`missions.LIST_LIMIT_MAX`). Up to this many rows come back from
 *  ONE request, and one request is one server statement over one snapshot — which is the only
 *  thing that actually proves a refresh read a consistent list (#896 review 16, finding 1). */
const SNAPSHOT_MAX = 200;

/** How many times a stitched refresh re-pages when a duplicate proves it read a torn snapshot.
 *  Bounded, because a rail that keeps re-fetching under a busy install is worse than one that
 *  says plainly it could not prove the read and offers to try again. */
const RELOAD_RETRIES = 2;

/** `rows` with anything already in `have` dropped, first occurrence winning.
 *
 *  **OFFSET PAGING IS NOT A SNAPSHOT** (#896 review 15, finding 2). The server orders the mutable
 *  set by `updated_at DESC`, so a mission touched between two page requests moves to the front
 *  and shifts everything after it down one. The client then receives one row TWICE and never
 *  receives the row that was pushed past the boundary. Both paths that page are exposed to it —
 *  LOAD MORE appends the next offset to what is on screen, and a refresh re-reads every open page
 *  — and in both the duplicate is what makes the COUNT lie: `rows.length` reaches `total` while a
 *  mission is missing, LOAD MORE disappears, and that mission is unreachable without a reload.
 *
 *  A duplicate id is exact proof, because within one consistent snapshot the server cannot return
 *  the same mission twice. Dropping it does not recover the missed row — nothing client-side can
 *  — but it keeps the count honest, and an honest count leaves LOAD MORE on screen, which is the
 *  way back to it. */
function dedupe(rows: MissionListRow[], have: MissionListRow[] = []) {
  const seen = new Set(have.map((m) => m.id));
  return rows.filter((m) => (seen.has(m.id) ? false : (seen.add(m.id), true)));
}

const STOPS = ["THREAD", "OBJECTIVES", "TIMELINE"] as const;

/** The states that have RELEASED the mission's roster (#896 review 20, finding 2). A mission in
 *  one of these holds no sessions and follows nothing through, so it cannot be the target of an
 *  adoption — the store refuses it, and the rail must not aim a control at a refusal. */
const CLOSED_STATES = new Set(["done", "failed", "abandoned"]);
type Stop = (typeof STOPS)[number];

/** One timeline row.
 *
 *  Most kinds are a label and a line of text. The two the composer produces are not: an operator
 *  message and the answer to it are a CONVERSATION, and `find` / `history` answer by naming
 *  sessions — an answer that names a session the operator cannot reach is half an answer, which
 *  is why the Ask box rendered the matches and why the durable turn carries them onto the event
 *  (#890). Model-derived text renders as TEXT; there is no `dangerouslySetInnerHTML` anywhere in
 *  these components. */
function ThreadEvent({ event }: { event: MissionEvent }) {
  const meta = (event.meta ?? {}) as { matches?: PulseAskMatch[] };
  const matches = Array.isArray(meta.matches) ? meta.matches : [];
  const label =
    event.kind === "operator_msg"
      ? "You"
      : event.kind === "assistant_msg"
        ? "Answer"
        : event.kind;
  return (
    <div className={styles.event} data-testid="thread-event">
      <div className={styles.eventHead}>{label}</div>
      <div className={styles.eventText}>{event.text ?? ""}</div>
      {matches.map((m) => (
        <div key={m.id} className={styles.matchRow} data-testid="ask-match">
          <div className={styles.eventText}>{m.title}</div>
          {m.why ? <div className={styles.objReason}>{m.why}</div> : null}
          <Link
            className={styles.openSession}
            to={sessionRoute(m.id)}
            aria-label={`Jump into ${m.title}`}
          >
            Jump in
          </Link>
        </div>
      ))}
    </div>
  );
}

/** Everything that belongs to ONE mission. Keyed by the parent — see the module note. */
function MissionBody({
  missionId,
  stop,
  configured,
  onTitle,
  onSessions,
  cards,
  onResolved,
  onNote,
  isCurrent,
  onMissionChanged,
}: {
  missionId: string;
  stop: Stop;
  configured: boolean;
  onTitle: (t: string | null) => void;
  onSessions: (keys: string[]) => void;
  /** UNFILTERED — a mission's decisions must not depend on the chips (see `allCards`). */
  cards: PulseCard[];
  onResolved: (a: OrchestratorAction) => void;
  onNote: (msg: string) => void;
  /** Forwarded to this body's composer. The durable one (#890) needs only the id: its turns are
   *  a mission's own rows, not console-global state, so there is no visit for it to outlive. */
  isCurrent: (missionId: string) => boolean;
  /** A lifecycle change landed (or was refused). The RAIL has to re-read too: closing, archiving
   *  or abandoning a mission changes the row the operator is looking at and, for archive, which
   *  scope it belongs in. `moved` says the mission left this rail entirely, and where it went. */
  onMissionChanged: (opts?: {
    /** Set only when THIS mount is still the one on screen — it clears the SELECTION. */
    scopeCleared?: boolean;
    /** WHICH mission moved and WHERE IT WENT, as one value. Global: never mount-fenced. */
    moved?: { id: string; to: "active" | "archived" };
    membershipChanged?: boolean;
  }) => void;
}) {
  const d = useMissionDetail(missionId);

  // Publish upward for the topbar and the UNTRACKED computation. In an effect rather than during
  // render, because it writes to a parent.
  const title = d.mission?.title ?? null;
  useEffect(() => {
    onTitle(title);
  }, [title, onTitle]);
  const keys = useMemo(
    () =>
      (d.mission?.sessions ?? [])
        .filter((s) => !s.removed_at)
        .map((s) => s.session_key),
    [d.mission],
  );
  useEffect(() => {
    onSessions(keys);
  }, [keys, onSessions]);

  /** The decisions waiting on THIS mission — the whole point of the console. A pending action
   *  rides its session's card (`_attach_pending`, #754), so the mission's decisions are the
   *  pending actions of the sessions it holds. Rendered with the same `ActionRow` the bell and
   *  the old grid used, so controls come from the SERVER's projection and are never re-derived
   *  here from `state`. */
  const decisions = useMemo(() => {
    const mine = new Set(keys);
    return cards
      .filter((c) => mine.has(c.id) && c.pending_action)
      .map((c) => c.pending_action as OrchestratorAction);
  }, [cards, keys]);

  /** THIS MOUNT, as an immutable fact — the fence every late outcome below is measured against
   *  (#896 review 10, finding 3).
   *
   *  It replaces `isCurrent(missionId)`, which asked the wrong question: an id says whether this
   *  mission is showing, and a late outcome only needs that to be true AGAIN. The body is keyed
   *  on the mission, so A1 → B → A2 is two different mounts of the same id; the id test admits
   *  A1's refusal into A2 and the operator sees a note about something they did before they left.
   *  A mount cannot be re-entered, so this cannot. */
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /** THE ONE FENCE for anything this body says out loud (#889).
   *
   *  `onNote` belongs to the CONSOLE, which outlives this body — so it is the one surface a late
   *  outcome can still reach after the operator has moved on. Everything else is safe by
   *  construction: the body is keyed on the mission, so a switch unmounts it and its own state
   *  writes land on a dead instance harmlessly.
   *
   *  So the fence lives HERE, wrapping the note, and every consumer gets the fenced version —
   *  the lifecycle bar included. Fencing only this file's own `mutate` was not enough and the
   *  browser gate caught it: `MissionLifecycle` owns its own error handling and called `onNote`
   *  directly, so a refused transition for mission A still appeared over mission B. One fence,
   *  at the boundary where the mission is known, rather than one per caller.
   *
   *  Liveness is read at RESOLUTION time, never captured: a boolean captured when the request
   *  started answers the question as it was at the moment that does not matter.
   *
   *  **AND THE MISSION IS PART OF LIVENESS** (#904 review 17, finding 2). Mount alone was not
   *  the fence this comment claims: switching missions does NOT unmount the console, so a late
   *  outcome for mission A passed a `mounted` check and was announced over mission B. Most
   *  consumers were saved by their own keyed remount and the dead-instance rule above; the plan
   *  card was not, because its await is a DISPATCH — the longest one on this screen, and the one
   *  the operator is most likely to walk away from.
   *
   *  The two halves are captured differently, and that is the whole point: the note's OWN mission
   *  is bound when the callback is made (it is what the note is about), and the CURRENT selection
   *  is read from a ref at resolution time (it is what may have changed). */
  const noteMission = d.mission?.id ?? null;
  const selectedRef = useRef<string | null>(null);
  useEffect(() => {
    selectedRef.current = noteMission;
  }, [noteMission]);
  const noteIfCurrent = useCallback(
    (msg: string) => {
      if (!mounted.current) return;
      if (noteMission !== null && selectedRef.current !== noteMission) return;
      onNote(msg);
    },
    [onNote, noteMission],
  );

  /** …and the same fence on the CONSEQUENCES a mutation asks the console to apply (#896 review 5,
   *  finding 3).
   *
   *  The re-read is unconditional and must stay that way: the mission changed on the server
   *  whoever happens to be looking at it. The EFFECTS are a different thing — a scope move
   *  tells the console the mission left this rail, and the console acts on that by clearing the
   *  selection. Applying that for a mission the operator has already navigated away from moves
   *  them off a DIFFERENT one, which is a late outcome reaching past its own mission exactly as
   *  a late note would. Same rule, same place, read at resolution time. */
  const changedIfCurrent = useCallback(
    (opts?: {
      movedTo?: "active" | "archived";
      membershipChanged?: boolean;
    }) => {
      // FENCED PER EFFECT, not as a whole (#896 review 6, finding 1). The previous version
      // dropped BOTH effects for a mission the operator had left, which is right for one of them
      // and wrong for the other:
      //
      // * clearing the SELECTION is mission-local — doing it for a mission nobody is looking
      //   at moves the operator off a different one;
      // * `membershipChanged` refreshes the OVERVIEW, which is global. Closing, archiving or
      //   detaching releases the mission's sessions server-side, and the cards keep their old
      //   `mission_id` until something re-reads them — so suppressing it leaves those sessions
      //   in neither the roster nor UNTRACKED until the outer poll happens to run. The operator
      //   having navigated away does not un-release them.
      const current = mounted.current;
      const next = {
        // VIEW-LOCAL: clearing the selection is fenced on this mount.
        ...(current && opts?.movedTo ? { scopeCleared: true } : {}),
        // GLOBAL, LIKE `membershipChanged` (#896 review 12, finding 1). WHICH MISSION MOVED is
        // a fact about the SERVER — it is in the other scope now — and it was riding on the
        // view-local half, so a late archive settling after the operator had walked away lost
        // the row removal and left the archived mission in the Active rail whenever the refresh
        // that followed it failed. The console cannot infer the id: `onMissionChanged` is
        // console-wide and the body is the only thing that knows whose lifecycle changed.
        //
        // …AND IT CARRIES THE DESTINATION WITH IT (#896 review 14). Forwarding the id without
        // the direction made the removal unanswerable: the console applied it to whichever rail
        // was on screen, so a held archive released after the operator switched to Archived
        // deleted the row from the rail the mission had just correctly arrived in. One value,
        // both halves, so the two can never be forwarded apart.
        ...(opts?.movedTo
          ? { moved: { id: missionId, to: opts.movedTo } }
          : {}),
        ...(opts?.membershipChanged ? { membershipChanged: true } : {}),
      };
      onMissionChanged(Object.keys(next).length ? next : undefined);
    },
    [missionId, onMissionChanged],
  );

  /** Operator edits and the stand-down share one busy flag and one re-read, because they are the
   *  same kind of act: a mutation whose OUTCOME the server owns. Both re-read unconditionally —
   *  on failure especially, since a 409 means this client's picture is the stale one. */
  const [mutating, setMutating] = useState(false);
  const mutate = useCallback(
    async (
      fn: () => Promise<unknown>,
      what: string,
      opts?: { membershipChanged?: boolean },
    ): Promise<boolean> => {
      // Returns whether the SERVER accepted it, so a caller holding the operator's typing can
      // keep it on a refusal (#896 review 6, non-blocking note). Clearing a draft the server
      // rejected makes the retry a retype.
      if (mutating) return false;
      setMutating(true);
      let ok = false;
      try {
        await fn();
        ok = true;
      } catch (err) {
        noteIfCurrent(
          err instanceof ApiError && err.message
            ? err.message
            : `${what} did not work.`,
        );
      } finally {
        setMutating(false);
        d.reload();
        // ON SETTLEMENT, not on success (#896 review 3, finding 4). A stale detach that 404s
        // because the session was already released still CHANGED what the operator sees: the
        // detail drops it from the roster while the overview card keeps its old `mission_id`, so
        // the session is in neither the roster nor UNTRACKED until the outer poll. A refresh is
        // cheap; a session that has vanished from both lists is not.
        // THE RAIL, ON EVERY MUTATION (#896 review 23, finding 3). Waiving, dropping,
        // reordering or standing down an objective can clear the very thing the rail derives
        // `needs_you` from — so re-reading only the DETAIL left the pane current and the rail
        // still saying the mission needs you, with no later list poll to reconcile them. The
        // membership refresh stays conditional; it is a different, global effect.
        changedIfCurrent(
          opts?.membershipChanged ? { membershipChanged: true } : undefined,
        );
      }
      return ok;
    },
    [mutating, noteIfCurrent, d, changedIfCurrent],
  );

  /** Editing is withdrawn on a mission nobody can act on any more.
   *
   *  ARCHIVED **and** TERMINAL — finding 5 from #896's review, where the comment said both and the
   *  check tested only the first. A `done` / `failed` / `abandoned` mission is a finished record;
   *  the routes would still take the write, which is exactly why the control has to be withdrawn
   *  here rather than relied on to be refused. Reopening is available from the lifecycle bar and
   *  is the honest way to edit a closed mission: it says the mission is open again.
   *
   *  The set is spelled out rather than imported from a shared constant because the client has no
   *  copy of the server's `TERMINAL_STATES` — and inventing one that silently drifts is worse than
   *  three literals with a test over all three. */
  const terminalState =
    d.mission?.state === "done" ||
    d.mission?.state === "failed" ||
    d.mission?.state === "abandoned";
  const editable =
    !!d.mission && d.mission.archived_at == null && !terminalState;

  const onOps = useCallback(
    (ops: ObjectiveOp[]) =>
      mutate(() => api.patchMissionObjectives(missionId, ops), "That edit"),
    [mutate, missionId],
  );
  const onStandDown = useCallback(
    (key: string, episode: number) =>
      void mutate(
        () => api.standDownObjective(missionId, key, episode),
        "Standing that objective down",
      ),
    [mutate, missionId],
  );
  /** Release a session from the mission. The overview is refreshed too, not just the mission: a
   *  released session becomes UNTRACKED, and that list is derived from the cards. */
  const onDetach = useCallback(
    (sessionKey: string) =>
      void mutate(
        () => api.detachMissionSession(missionId, sessionKey),
        "Releasing that session",
        // The released session has to reappear under UNTRACKED, and that list comes from the
        // overview's cards rather than from the mission list — on settlement, success or not.
        { membershipChanged: true },
      ),
    [mutate, missionId],
  );

  const objectives = (
    <ObjectivesPane
      objectives={d.objectives}
      objectivesState={d.mission?.objectives_state}
      context={d.context}
      loading={!d.context}
      supervisor={d.mission?.supervisor}
      onMembershipChanged={d.reloadContext}
      onOps={editable ? onOps : undefined}
      onStandDown={editable ? onStandDown : undefined}
      onDetach={editable ? onDetach : undefined}
      busy={mutating}
    />
  );
  const timeline = (
    <TimelinePane
      events={d.events}
      cursor={d.cursor}
      loadingMore={d.loadingMore}
      onLoadMore={d.loadOlder}
    />
  );

  return (
    <>
      <div className={styles.pane} data-testid="pane">
        {/* The mission's own controls, above its content and on every stop — closing a mission
            from the timeline is as reasonable as closing it from the thread, and hiding them
            behind one stop would make the control depend on where you happened to be. */}
        {d.mission ? (
          <MissionLifecycle
            mission={d.mission}
            onChanged={(opts) => {
              d.reload();
              changedIfCurrent(opts);
            }}
            onNote={noteIfCurrent}
          />
        ) : null}
        {stop === "THREAD" ? (
          <>
            {!configured ? (
              <div className={styles.notice} data-testid="no-ai-notice">
                <div className={styles.noticeLead}>
                  No AI endpoint configured.
                </div>
                <div>
                  Off: suggestions, recaps, progress, completion proposals, and
                  the composer. Still works: create, adopt, objectives,
                  timeline, approvals.
                </div>
              </div>
            ) : null}
            {/* THE QUESTION FIRST, above the thread and above the composer — it is the thing
                the mission is waiting on, and burying it under the history would make
                `needs_you` point at something the operator has to scroll to find (#892). It
                outranks the proposal for the same reason: a question is already open, while a
                plan is something to start. */}
            {d.mission?.question ? (
              /* KEYED ON THE QUESTION'S OWN SEQ, not on the slot (#900 review, finding 5). A
                 question is superseded in place: the answer 409s, the reload lands question B in
                 the same prop, and an unkeyed card keeps its state across the swap — so the free
                 text the operator typed about A is sitting in the box, enabled, over B. The key
                 makes the replacement a remount, which is the only thing that reliably clears
                 state a child owns. */
              <MissionQuestionCard
                key={d.mission.question.seq}
                missionId={missionId}
                question={d.mission.question}
                onAnswered={d.reload}
                // THE SAME FENCE AS EVERY OTHER CONSUMER (#896 review 23, finding 4). An
                // answer settles asynchronously — a 409 for a superseded question, or
                // `applied_ok: false` — and the raw callback let mission A's refusal paint
                // over mission B after the operator had navigated. This is what makes the
                // claim above ("every consumer gets the one fence") true rather than nearly.
                onNote={noteIfCurrent}
              />
            ) : null}
            {/* THE PROPOSAL (#893 Phase 4). The only thing on this screen asking the operator
                for a decision that starts an agent — and it renders nothing at all once the
                mission has left the planning states. */}
            {d.mission ? (
              <MissionPlanCard
                key={d.mission.plan?.plan_id ?? "no-plan"}
                mission={d.mission}
                // BOTH HALVES, like every other mutation on this screen (#904 review 18, finding
                // 2). `d.reload` re-reads the mission DETAIL and nothing else, so planning left
                // the rail row saying `draft` next to a pane showing the plan, and a dispatch
                // left the session it had just attached sitting in UNTRACKED until the next
                // supervisor poll. The rule is stated forty lines up and this was the one
                // surface that did not follow it.
                onChanged={(opts) => {
                  d.reload();
                  changedIfCurrent(opts);
                }}
                // THROUGH THE MOUNT FENCE, like every other async consumer (#904 review 17,
                // finding 2). A dispatch is the LONGEST await on this screen — it launches a
                // process — and the operator is free to move to another mission while it runs.
                // The raw callback here made the claim four lines above ("every consumer gets
                // the one fence") false by exactly one component: mission A's failure was
                // rendered on mission B's pane.
                onNote={noteIfCurrent}
              />
            ) : null}
            {decisions.map((a) => (
              <ActionRow
                key={a.id}
                action={a}
                onResolved={onResolved}
                // FENCED, like every other note this body emits (finding 6 from #896's review).
                // `ActionRow` calls this from its own 409 path — a compare-and-execute that lost
                // carries the settled record — so an approval decided on mission A and refused
                // while the operator moved to B would otherwise show A's refusal over B. The
                // browser test that caught the lifecycle instance of this now covers this path
                // too; it is the same bug one consumer further along.
                onNote={noteIfCurrent}
              />
            ))}
            {d.events.length === 0 && decisions.length === 0 ? (
              <div className={styles.empty}>Nothing has happened yet.</div>
            ) : (
              d.events.map((e) => <ThreadEvent key={e.seq} event={e} />)
            )}
            {/* THE COMPOSER SENDS DURABLE TURNS (#890). The transcript above IS the mission
                timeline — the route writes the operator's message in its claim transaction and
                the answer in its settlement — so the composer owns only the draft and the turn
                in flight. */}
            <MissionComposer
              missionId={missionId}
              configured={configured}
              isCurrent={isCurrent}
              onSettled={d.reload}
              detail={d.mission ?? null}
            />
          </>
        ) : stop === "OBJECTIVES" ? (
          objectives
        ) : (
          timeline
        )}
      </div>

      {/* The persistent detail column at ≥1400px. It STACKS objectives, context and the timeline
          — which is why mobile splits at that one seam into three stops rather than four: the
          column is not three separate things. */}
      <aside className={styles.detail} aria-label="Mission detail">
        {objectives}
        <div className={styles.section}>Timeline</div>
        {timeline}
      </aside>
    </>
  );
}

export function MissionConsole({
  cards,
  allCards,
  configured,
  loading,
  onActionResolved,
  onMembershipChanged,
  filtered,
  onClearFilters,
}: {
  /** Live sessions from the existing overview, AFTER the project/agent chips. This narrows the
   *  UNTRACKED list and nothing else. */
  cards: PulseCard[];
  /** The same set BEFORE the chips. A mission's decisions are read from here, because a filter
   *  is a view over the session list and was never meant to withdraw a decision: selecting a
   *  chip that excludes a held session must not remove its Approve/Reject row while the mission
   *  stays selected and still says `needs_you`. */
  allCards: PulseCard[];
  /** Told when a decision settles, so the route can refresh the overview the cards came from. */
  onActionResolved?: (a: OrchestratorAction) => void;
  /** Refetch the overview. Adoption changes WHICH MISSION HOLDS a session, and that fact is now
   *  stamped on the card by the server — so the cards are the thing that went stale, not just
   *  the mission list. Refreshing only the list would leave the adopted session sitting in
   *  UNTRACKED, still offering ADOPT, until the next poll. */
  onMembershipChanged?: () => void;
  /** Whether an AI endpoint is configured. The composer is disabled without one —
   *  `/api/pulse/ask` answers 409 and has no local fallback, so `find` / `history` genuinely do
   *  not work without a model. */
  configured: boolean;
  /** The overview scan is still running. Only affects the UNTRACKED group's honesty: "no live
   *  sessions" and "we have not looked yet" are different claims. */
  loading?: boolean;
  /** The project/agent chips are narrowing the list right now. Needed to tell "you have no
   *  sessions" from "this COMBINATION has none" — the second is recoverable in one tap and the
   *  first is not, and saying the wrong one leaves the operator staring at a blank pane with no
   *  way back (#803). */
  filtered?: boolean;
  onClearFilters?: () => void;
}) {
  /** THE RAIL'S ROWS AND ITS COUNT, as ONE value (#896 review 13, finding 1).
   *
   *  "How many are there" is a fact about the same list the rows came from, and holding them in
   *  two `useState`s let them disagree — a local removal decremented the count whether or not it
   *  removed anything, and `rows.length >= total` then hid LOAD MORE over a mission that is
   *  still there. Every write below sets both halves in one updater. */
  const [list, setRail] = useState<{
    rows: MissionListRow[];
    total: number;
    /** HOW MANY SERVER ROWS HAVE BEEN CONSUMED, which is not `rows.length` (#896 review 17,
     *  finding 1). Deduping drops rows the server DID return, so using the rendered count as the
     *  next offset asks for a row already on screen — and LOAD MORE then sticks there for ever,
     *  one short, with the missing mission unreachable. The cursor counts what was asked for and
     *  answered; the rows are what survived the merge. */
    consumed: number;
    /** The last read was STITCHED from several pages, so it cannot prove it saw one snapshot
     *  (#896 review 17, finding 2). The rail offers a re-read rather than pretending. */
    stitched?: boolean;
    /** WHICH ordered set the cursor counts into — the server's digest of the full filtered list
     *  (#896 review 19, finding 1). A later page is a continuation of this read only if it was
     *  cut from the same one; otherwise the offsets index into two different lists and the rail
     *  cannot be proved whole, however consistent its counts look. */
    snapshot?: string | null;
  }>({ rows: [], total: 0, consumed: 0 });
  const missions = list.rows;
  const [storeError, setStoreError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [stop, setStop] = useState<Stop>("THREAD");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [adopting, setAdopting] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  const [heldExtra, setHeldExtra] = useState<string[]>([]);
  /** Ask turns for the UNTRACKED view ONLY (#890). A mission's turns are durable and live in its
   *  timeline; this view has no mission to keep them in, so its Ask stays transient and says so
   *  on screen. Keyed by view rather than kept bare, because the sentinel is one of several
   *  things the console can be showing. */
  const [turns, setTurns] = useState<Record<string, AskTurn[]>>({});

  const drawerBtnRef = useRef<HTMLButtonElement | null>(null);
  /** What is selected RIGHT NOW, for callbacks that resolve later. A ref rather than the state
   *  value because a captured boolean answers the question as it was when the request started,
   *  which is exactly the moment that does not matter. */
  const shownRef = useRef<string | null>(null);
  /** …and WHICH VISIT to it this is (#896 review 10, findings 3 and 4).
   *
   *  An id answers "is this view showing?", and a late outcome only needs that to be true AGAIN.
   *  Selecting A, leaving for B and coming back to A is three visits, two of them over — and an
   *  id-equality fence admits a request issued in the first one into the third, which is the
   *  A1 → B → A2 round trip the id test cannot see.
   *
   *  The counter also moves on a SCOPE change, which is finding 4: the UNTRACKED composer is not
   *  unmounted by the Active → Archived flip and keeps the same sentinel id, so without the scope
   *  in the identity a create begun in Active still resolves as `focus: true` while Archived is
   *  on screen — selecting an active mission into an archived rail.
   *
   *  Captured at SEND time and compared at RESOLUTION time. Both halves are load-bearing:
   *  capturing the ANSWER rather than the token is the bug this replaces. */
  const visitRef = useRef(0);
  /** …and the same number as STATE, for the one consumer that cannot capture it itself.
   *
   *  `ActionRow` calls `onNote` at RESOLUTION time and takes no token — it is shared with the
   *  bell and has no notion of a console visit. What it does do is hold the `onNote` prop its
   *  in-flight handler was created with, so a callback whose identity changes per visit carries
   *  the visit with it. That is what the state is for: the ref cannot change a closure. */
  const [visitTk, setVisitTk] = useState(0);
  // `?? UNTRACKED_VIEW` is load-bearing, not defensive. With nothing selected the console still
  // renders a composer — that is the empty state, and Ask must work on a fresh install — and its
  // `missionId` is the sentinel. Comparing against a null selection would make that composer
  // never current, so every answer it received would be discarded as stale: the fence firing on
  // the one surface it was never meant to guard.
  const isCurrent = useCallback(
    (id: string) => (shownRef.current ?? UNTRACKED_VIEW) === id,
    [],
  );
  /** The visit to capture. Read from an event handler — after the effect below has published it
   *  — never during a render, where the effect for the current selection has not run yet. */
  const visit = useCallback(() => visitRef.current, []);
  /** Strictly stronger than `isCurrent`: the counter moves on every view AND scope change, so an
   *  equal token already implies the same view is showing, in the same visit, in the same scope. */
  const isVisitCurrent = useCallback(
    (at: number) => at === visitRef.current,
    [],
  );

  /** The same fence the mission body's notes take, for the UNTRACKED view's own decisions.
   *
   *  That view is not inside the keyed mission body, so selecting a mission does not unmount it
   *  and a late `ActionRow` refusal would paint over whatever the operator moved to.
   *
   *  **On the VISIT, not the view's id** (#896 review 11, finding 2). `isCurrent(UNTRACKED_VIEW)`
   *  is true again the moment the operator comes back, so UNTRACKED → mission B → UNTRACKED
   *  admitted a settled-action 409 raised two views ago into a list that has since been re-read:
   *  "Not sent — already settled", about something the operator did before they stepped out.
   *
   *  The token is captured HERE, in the closure `ActionRow` holds — because that component
   *  reports at resolution time and cannot capture one itself. A new callback identity per visit
   *  is the capture. */
  const noteIfUntracked = useCallback(
    (msg: string) => {
      if (!isVisitCurrent(visitTk)) return;
      setNote(msg);
    },
    [isVisitCurrent, visitTk],
  );

  const total = list.total;
  /** …and how many server rows have been consumed, which is the next page's offset. */
  const consumed = list.consumed;
  /** ONE SOURCE FOR THE AFFORDANCE AND THE HANDLER (#896 review 18). The rail used to render
   *  LOAD MORE on `rows.length < total` while `loadMoreMissions` stopped on `consumed >= total` —
   *  and dedupe is exactly what drives those apart: 299 rendered, 300 consumed, total 300. The
   *  button was visible and the handler returned immediately, so the missing mission stayed
   *  unreachable behind a control that looked like the way to it. */
  const hasMore = consumed < total;
  /** …and when there is nothing left to consume and the rail is STILL short, the pages did not
   *  agree. An offset append cannot fill that hole — only a fresh read can — so the honest
   *  affordance is the re-read, not a button that would ask past the end. */
  const needsReRead =
    Boolean(list.stitched) || (!hasMore && missions.length < total);
  const [loadingMore, setLoadingMore] = useState(false);
  /** Which scope the rail lists. Archiving is not deletion, so there has to be a way back in:
   *  without one, a mission's objectives, timeline and decisions become unreachable the moment
   *  it is put away. */
  const [archived, setArchived] = useState(false);
  /** Which SCOPE a list response belongs to, as a monotonic counter.
   *
   *  A list request outlives the scope it was issued for. "Load more" on the active rail can
   *  still be in flight when the operator switches to Archived, and its response then APPENDS —
   *  50 active missions land in the archived rail and overwrite its total, with nothing on
   *  screen saying the two sets were mixed. Clearing the list on the switch does not help: the
   *  clear happens first and the stale append lands after it.
   *
   *  A generation, not a captured `archived` boolean: switching away and back would make a
   *  captured boolean match again, so the stale response would be accepted by a fence that
   *  looks like it is working. Every list write checks the generation it was issued under. */
  const listGen = useRef(0);
  /** The highest generation whose response has been APPLIED.
   *
   *  `listGen` alone only fences a SCOPE change, so every same-scope request — the mount, a
   *  post-mutation `reload()`, a Load More page — shared one generation and the LAST to arrive
   *  won regardless of when it was issued. A mount response landing after a refresh restored the
   *  pre-mutation rows, quietly undoing the refresh matrix this console depends on
   *  (#896 review 3, finding 2). Every authoritative refresh now takes its own epoch and an older
   *  response is dropped rather than applied. */
  const appliedGen = useRef(0);
  /** The scope a request must have been issued for. `reload` is a callback and its captured
   *  `archived` can be a render behind the rail — so a late create's `reload()` issued an Active
   *  request that was then accepted into the Archived scope (finding 3). Read at ISSUE time. */
  const archivedRef = useRef(false);
  /** …and HOW MANY ROWS ARE OPEN, for exactly the same reason (#896 review 25, finding 1). A
   *  refresh re-reads the pages the operator has opened, and a callback captured before they
   *  opened another one would re-read fewer — collapsing the rail rather than refreshing it.
   *
   *  **The window the operator has ASKED FOR, moved at ISSUE time — never a mirror of what has
   *  rendered** (review 26, finding 1). Mirroring `missions.length` in a passive effect left a
   *  gap exactly one paint wide: LOAD MORE issues its request, and until that page comes back
   *  and React commits it the ref still says 100. A create settling inside that gap — the
   *  ordinary case, since the click is what the operator does WHILE waiting — refreshed the old
   *  window and collapsed the rail, which is the very bug this ref was added for. So the ask
   *  raises it synchronously, and only an authoritative answer sets it back to what exists. */
  const openRef = useRef(PAGE);
  /** …and WHEN it was last raised, so an OLDER answer cannot narrow a NEWER ask (review 26,
   *  finding 1, inverse ordering). A refresh issued for 100 rows is in flight; the operator
   *  clicks LOAD MORE and the window becomes 200; then the older refresh returns and, reading
   *  only its own width, set the window back to 100 — erasing an intent that was recorded after
   *  it was issued. The generation the ask was made at is the whole comparand: an answer may
   *  narrow the window only when nothing was asked for at or after its own generation. */
  const openAskGen = useRef(0);

  const nextGen = useCallback(() => {
    listGen.current += 1;
    return listGen.current;
  }, []);

  const applyList = useCallback(
    (
      gen: number,
      scope: boolean,
      r: {
        missions: MissionListRow[];
        total?: number;
        store_error?: string | null;
        consumed?: number;
        stitched?: boolean;
        snapshot?: string | null;
      },
    ) => {
      // Two fences, and they answer different questions: `gen` is "is this the newest answer",
      // `scope` is "is this even about the rail we are showing".
      //
      // AGAINST THE NEWEST *ISSUED* GENERATION, not the newest applied one (#896 review 23,
      // finding 2). `appliedGen` only advances on SUCCESS, so a refresh that FAILED left it
      // where it was — and an older mount response arriving afterwards compared equal and was
      // accepted, resurrecting a row the operator had just archived until some later refresh
      // happened to succeed. The failure path already fences on `listGen` for exactly this
      // reason; the success path was the half that did not.
      if (gen < listGen.current || scope !== archivedRef.current) return;
      appliedGen.current = gen;
      // …AND THE WINDOW IS WHAT THIS ANSWER OPENED. An authoritative read replaces the rail, so
      // it also decides how wide the rail now is — including narrower, when the scope changed or
      // rows were archived elsewhere. `consumed` rather than the merged length: it is what the
      // SERVER sent, so a dedupe inside one read cannot ratchet the window down a row at a time.
      //
      // NARROWER ONLY IF NOTHING WAS ASKED FOR SINCE THIS ANSWER WAS ISSUED (review 26, inverse
      // ordering). A LOAD MORE recorded at or after `gen` is a newer statement of what is open
      // than this answer can be, and letting the answer win discarded it.
      const width = Math.max(PAGE, r.consumed ?? r.missions.length);
      openRef.current =
        openAskGen.current >= gen ? Math.max(openRef.current, width) : width;
      const answered = r.consumed ?? r.missions.length;
      const snapshot = r.snapshot ?? null;
      setRail((prev) => {
        // A NARROWER ANSWER DOES NOT UNMAKE A PROVEN CONTINUATION (#896 review 28, finding 1).
        // An authoritative read replaces the rail, and that is right when it is an answer about a
        // DIFFERENT list. But a refresh issued for 100 rows, landing after the operator's page
        // brought the rail to 200, was replacing rows 0–199 of a list with rows 0–99 of the SAME
        // list — the pages visibly collapsed back, which is the review-25 symptom arriving by a
        // third route. The digest covers the whole filtered set, so when it agrees, the rows this
        // answer does not carry are still exactly the rest of it and are kept behind it.
        //
        // When the digest DISAGREES the list has moved and the answer is the whole truth about
        // it: replaced outright, because keeping a tail from a list that no longer exists is how
        // an archived mission walks back onto the rail.
        //
        // BEYOND THE WINDOW, not merely ABSENT FROM THE ANSWER. Those are different sets and the
        // difference is a resurrection: a row REMOVED from inside the answer's own window is also
        // "in the rail and not in the answer", so keeping it would put an archived mission back
        // on the rail — the exact defect the digest is here to prevent. The rail only holds
        // anything this answer cannot speak for when its cursor reaches FURTHER than the answer's.
        const same = snapshot !== null && snapshot === prev.snapshot;
        const tail =
          same && prev.consumed > answered
            ? dedupe(prev.rows.slice(r.missions.length), r.missions)
            : [];
        return {
          rows: tail.length ? [...r.missions, ...tail] : r.missions,
          total: r.total ?? r.missions.length,
          // An authoritative answer RESETS the cursor: it is a fresh read of the window, so what
          // has been consumed is exactly what it returned — plus whatever of the same snapshot
          // the rail is still holding beyond it.
          consumed: tail.length ? Math.max(answered, prev.consumed) : answered,
          stitched: r.stitched,
          // THE SNAPSHOT THE CURSOR INDEXES INTO. A later page is only a continuation of this
          // read if it was cut from the same one (#896 review 19, finding 1).
          snapshot,
        };
      });
      setStoreError(r.store_error ?? null);
    },
    [],
  );

  useEffect(() => {
    archivedRef.current = archived;
    let live = true;
    const gen = nextGen();
    api
      .missions({ limit: PAGE, archived })
      .then((r) => live && applyList(gen, archived, r))
      // A read that fails empties the rail and SAYS SO; it never takes the console down, and it
      // is never conflated with "you have no missions".
      //
      // …and the FAILURE takes the same ownership test as the success (#896 review 9, finding
      // 3). `live` is the effect's lifetime, which is a different question: a mount request held
      // open while a mutation's reload installs fresh rows and clears the error is still `live`
      // when it rejects, and it painted a store outage over data that had just arrived.
      .catch(() => {
        if (!live) return;
        // FENCED ON THE LATEST ISSUED GENERATION, not the applied one (#896 review 10, finding
        // 1). `appliedGen` only advances on SUCCESS, so a failure of the current request — the
        // ordinary case, and the one this notice exists for — compared its own `gen` against a
        // lower applied value and suppressed itself. The rail then painted "Nothing tracked
        // yet" over a store that would not answer, which is the exact conflation the notice was
        // written to prevent. The question here is "has a NEWER request been issued", and
        // `listGen` is what answers it.
        if (gen !== listGen.current || archived !== archivedRef.current) return;
        setStoreError("the mission list could not be loaded");
      });
    return () => {
      live = false;
    };
  }, [applyList, archived, nextGen]);

  /** Refresh the pages the operator has already opened.
   *
   *  One request per page, NOT one big request: the server clamps a page to `LIST_LIMIT_MAX`
   *  (200), so asking for `missions.length` silently returned the first 200 once more than 200
   *  rows were open — a refresh after an adopt would then delete the later pages from the rail,
   *  which looks exactly like the missions being gone. Paging back over what is open cannot
   *  drop anything, at the cost of one round trip per 100 rows the operator chose to load.
   *
   *  `total` comes from the LAST page, so a mission created or archived mid-refresh is still
   *  reflected in the "Load more" affordance. */
  const reload = useCallback(async () => {
    // THE WINDOW AT ISSUE TIME, from the ref — not the `missions.length` this callback closed
    // over (#896 review 25, finding 1). An async child holds a `reload` across its own await:
    // the composer's create resolves and calls `onCreated`, which reloads. If the operator opened
    // another page while that request was in flight, the captured length is the OLD one, so the
    // refresh asks for 100 rows and REPLACES the 200-row rail — every opened page gone, and a
    // selected mission from the later page with it. Same reasoning as the scope ref two lines
    // down, and the same failure it was written for.
    const open = Math.max(PAGE, openRef.current);
    // The scope at ISSUE time, from the ref — not the `archived` this callback closed over, which
    // can be a render behind. A late create's `reload()` otherwise requested the scope that was
    // current when its closure was made and had the answer accepted into the one on screen
    // (#896 review 3, finding 3).
    const scope = archivedRef.current;
    const gen = nextGen();

    // ONE REQUEST IS THE ONLY PROOF (#896 reviews 16 and 17).
    //
    // Deduplicating page overlaps catches a REORDER, because a repeated id cannot happen inside
    // one snapshot. It cannot catch a REMOVAL: another client archives M50 between page 0 and
    // page 1, offset 100 then starts one row later, and the merged rail has 199 unique rows —
    // no duplicate, `total` 199, LOAD MORE hidden — while still holding the stale M50 and
    // permanently missing M101. A duplicate is proof of tearing; the absence of one is not proof
    // of a snapshot.
    //
    // The server clamps a page at `SNAPSHOT_MAX`, so up to that size the whole open window comes
    // back from a single statement over a single snapshot and there is nothing to stitch.
    if (open <= SNAPSHOT_MAX) {
      try {
        const r = await api.missions({ limit: open, archived: scope });
        if (scope !== archivedRef.current) return;
        applyList(gen, scope, {
          missions: dedupe(r.missions),
          total: r.total,
          store_error: r.store_error,
          consumed: r.missions.length,
          snapshot: r.snapshot ?? null,
        });
      } catch {
        /* A failed refresh leaves the rail as it stands; it never empties it. */
      }
      return;
    }

    const pages = Math.ceil(open / PAGE);

    /** One pass over the open pages. `torn` says the pages did not come from one snapshot. */
    const fetchPages = async () => {
      const results = [];
      for (let i = 0; i < pages; i += 1) {
        results.push(
          await api.missions({
            limit: PAGE,
            offset: i * PAGE,
            archived: scope,
          }),
        );
        // Checked between pages as well as at the end: a multi-page refresh is the request most
        // likely to still be running when the operator switches scope.
        if (scope !== archivedRef.current) return null;
      }
      const fetched = results.flatMap((r) => r.missions);
      const rows = dedupe(fetched);
      // TORN IS NOW A PROOF (#896 review 19, finding 1). A repeated id proves the pages did not
      // come from one snapshot; the ABSENCE of one proves nothing, because a removal between
      // pages shifts every later offset back by one and leaves no duplicate behind — a rail with
      // 199 unique rows, `total` 199, a stale M50 still on it and M101 gone for good.
      //
      // The server now says which ordered set each page was cut from, so equal digests across
      // every page is the guarantee the offsets needed. A page with no digest is a degraded read
      // and counts as a mismatch, never as agreement.
      const snap = results[0].snapshot ?? null;
      const oneSnapshot =
        snap !== null && results.every((r) => (r.snapshot ?? null) === snap);
      return {
        rows,
        torn: rows.length !== fetched.length || !oneSnapshot,
        consumed: fetched.length,
        total: results[results.length - 1].total,
        store_error: results[results.length - 1].store_error,
        snapshot: snap,
      };
    };

    try {
      // …and a torn read is RETRIED rather than shown. The window is small and uncorrelated with
      // this client, so a second pass almost always lands consistent.
      let out = await fetchPages();
      for (
        let attempt = 0;
        out?.torn && attempt < RELOAD_RETRIES;
        attempt += 1
      ) {
        out = await fetchPages();
      }
      if (!out) return;
      applyList(gen, scope, {
        missions: out.rows,
        total: out.total,
        store_error: out.store_error,
        consumed: out.consumed,
        // A STITCHED READ THAT COULD NOT BE PROVED (#896 reviews 17 and 18). Every opened page
        // is kept — dropping them is the failure a previous review was about — but a read that
        // is still torn after its retries drives something the operator can ACT on rather than a
        // LOAD MORE that would return immediately: a RE-READ. An offset append can never fill an
        // interior hole; only a fresh read can.
        //
        // `out.torn`, not `true`: paging is not itself a defect. A multi-page read whose pages
        // agreed is complete — and now it can PROVE it agreed, which is what makes narrowing the
        // control honest rather than optimistic. Saying "unproven" of every stitched read would
        // leave it on screen for ever on any rail past the server's one-page cap.
        stitched: out.torn,
        snapshot: out.snapshot,
      });
    } catch {
      /* A failed refresh leaves the rail as it stands; it never empties it. */
    }
  }, [applyList, nextGen]);

  /** Follow the list rather than hard-capping it. The rail's contract is "every mission", and
   *  the first version stopped at 100 with no continuation — so mission 101 was unreachable
   *  with nothing on screen to say so. One explicit page at a time, because the alternative
   *  (fetch until exhausted on mount) makes an install with a long history pay for rows nobody
   *  asked to see. */
  /** The newest pagination attempt. An attempt owns the busy flag and the append only while it
   *  is still this one — see `loadMoreMissions`. */
  const pageAttempt = useRef(0);
  /** …and the newest ADOPT, for the same reason, plus the view it was started from. */
  const adoptAttempt = useRef(0);

  const loadMoreMissions = useCallback(() => {
    // THE CURSOR, not the rendered count (#896 review 17, finding 1). Deduping drops rows the
    // server DID return, so `missions.length` is short of what has been consumed — asking for it
    // as the next offset re-requests a row already on screen, and the rail sticks there for ever
    // with the mission that moved ahead of page 0 unreachable.
    if (loadingMore || consumed >= total) return;
    setLoadingMore(true);
    // THE ASK, RECORDED BEFORE THE REQUEST GOES OUT (#896 review 26, finding 1). This is the
    // moment the operator opened another page; waiting for the answer to paint would leave a
    // refresh issued in between re-reading the narrower window and throwing this page away.
    openRef.current = Math.max(openRef.current, consumed + PAGE);
    // …STAMPED WITH THE NEWEST ISSUED GENERATION, so an answer already in flight cannot undo it.
    openAskGen.current = listGen.current;
    const scope = archivedRef.current;
    // AN IMMUTABLE TOKEN PER ATTEMPT, owned by both the apply and the cleanup (#896 review 9,
    // finding 5). The scope alone is not ownership: it round-trips. A1 can still be in flight
    // while the operator visits Archived and comes back, and A2 starts — then A1 settles, sees
    // its own scope again, and clears A2's busy flag. A3 then starts at the same offset, shares
    // A2's base and generation, and both append the same rows.
    const attempt = ++pageAttempt.current;
    // AN APPEND TAKES NO GENERATION OF ITS OWN (#896 review 4, finding 2).
    //
    // It is not an authoritative answer about the rail — it extends whatever the newest
    // authoritative answer was. Allocating a generation made it outrank one: a page issued
    // before a mutation and arriving after that mutation's reload advanced `appliedGen` past
    // the reload, which `applyList` then dropped. The rail kept its stale first page — a
    // just-archived or just-closed row still on it — with a fresh page appended underneath.
    //
    // So it records what it is an extension OF, and answers to that instead.
    const base = appliedGen.current;
    const issued = listGen.current;
    api
      .missions({ limit: PAGE, offset: consumed, archived: scope })
      .then((r) => {
        // Three questions, and an append has to answer all three:
        //
        // * is this even the rail we are showing — without it a page issued against the active
        //   scope lands in the archived one and mixes the two sets;
        // * has the list this page was computed as an OFFSET INTO been replaced since — if it
        //   has, this page names rows from a list that no longer exists;
        // * has an authoritative refresh been ISSUED since — because its answer is the one that
        //   must win whenever it lands, and an append that slipped in first would be silently
        //   overwritten by it anyway.
        // * …and is this still the attempt anyone is waiting on, which a scope that has been
        //   round-tripped cannot answer.
        if (attempt !== pageAttempt.current) return;
        if (scope !== archivedRef.current) return;
        if (listGen.current !== issued) return;
        // HAS THE LIST THIS PAGE IS AN OFFSET INTO BEEN REPLACED SINCE? Read at ARRIVAL, which is
        // when the question has an answer — the merge below only decides what to do about it.
        //
        // This used to be the whole test, and it was a PROXY (review 26, inverse ordering). A
        // refresh issued before the operator clicked LOAD MORE, landing after it, advances
        // `appliedGen` and the page was thrown away — the click lost, with nothing on screen to
        // say so. But the server names the ordered set every page was cut from, and that digest
        // covers the whole filtered list rather than the slice: page 0 and page 1 of one list
        // carry the SAME digest. So "is this a continuation of what is on the rail" has a real
        // answer now, and the proxy is only needed where the digest cannot say.
        const replaced = appliedGen.current !== base;
        // DEDUPED AGAINST WHAT IS ALREADY ON SCREEN, not merely within the page. An append is
        // the other half of the torn-snapshot problem and the one the operator meets first: the
        // page is an OFFSET INTO a list that may have reordered since the page before it, so its
        // first rows can be rows the rail already has.
        setRail((prev) => {
          // AN OFFSET INTO A LIST THAT MAY HAVE MOVED (#896 review 19, finding 1). The append
          // has exactly the tearing problem the refresh has, and the same proof settles it: this
          // page is a continuation only if it was cut from the set the cursor counts into. When
          // it was not, the rows are still kept — dropping what the server sent is the failure an
          // earlier review was about — and the rail says the list could not be proved whole, so
          // the operator is offered the RE-READ that can actually close an interior hole.
          const same =
            (r.snapshot ?? null) !== null &&
            (r.snapshot ?? null) === prev.snapshot;
          // REPLACED AND UNPROVEN IS THE ONE CASE THAT IS DROPPED. Keeping it would append rows
          // from a list that no longer exists — a mission archived elsewhere walks back onto the
          // rail — which is worse than losing a page the operator can ask for again. Replaced but
          // PROVEN is an ordinary continuation, and unreplaced keeps the review-19 behaviour
          // exactly: the rows are kept and the rail says it could not be proved whole.
          if (replaced && !same) return prev;
          return {
            ...prev,
            rows: [...prev.rows, ...dedupe(r.missions, prev.rows)],
            total: r.total ?? prev.total,
            // …and the cursor advances by what the SERVER sent, whether or not the merge kept it.
            consumed: prev.consumed + r.missions.length,
            stitched: prev.stitched || !same,
            snapshot: r.snapshot ?? null,
          };
        });
      })
      .catch(() => undefined)
      .finally(() => {
        // The SAME ownership test. A superseded attempt unlocking the current one is how two
        // requests end up at one offset.
        if (attempt === pageAttempt.current) setLoadingMore(false);
      });
  }, [loadingMore, consumed, total]);

  /** Live sessions no mission holds. `heldExtra` folds in the selected mission's own roster so a
   *  freshly adopted session leaves UNTRACKED immediately, without waiting for the list refetch.
   *  Computed from the missions we have, so a store outage shows every live session as untracked
   *  rather than hiding them — the safe direction: the operator can still see their work, and it
   *  is adoption that is unavailable. */
  const untracked = useMemo(() => {
    // OWNERSHIP COMES FROM THE CARD, not from the mission rows in memory. The rail is paged, so
    // deriving it here made "held by mission 101" indistinguishable from "held by nobody" until
    // the operator clicked Load more — and the session was offered an ADOPT the server refused
    // with 409. `mission_id` is stamped server-side over EVERY mission (`routes/pulse`), loaded
    // or not.
    //
    // `heldExtra` still folds in the selected mission's freshly-read roster, so a just-adopted
    // session leaves UNTRACKED on the spot rather than on the next overview poll. The mission
    // rows are no longer consulted at all.
    const held = new Set<string>(heldExtra);
    // NOT filtered on `c.live`. #840 §15 says "live sessions with no mission", but the overview's
    // cards are RECENT work and most carry `live: false` — that flag is a registry overlay
    // (working/attached), not "exists". Filtering on it hid almost everything the grid used to
    // show, including settled sessions whose last action the operator still wants to see. The
    // rail replaces the grid, so it lists what the grid listed; `live` drives the dot, not
    // membership.
    return cards.filter((c) => !held.has(c.id) && !c.mission_id);
  }, [cards, heldExtra]);

  /** What the console opens on when the operator has not chosen yet — DERIVED, never stored.
   *
   *  It opens on WHAT NEEDS YOU. A decision the operator has to go looking for is a decision
   *  they will miss, which is the failure this whole feature exists to remove. So: a mission
   *  whose ledger says `needs_you` first, then an untracked session carrying a pending decision,
   *  then simply the first mission.
   *
   *  Derived rather than written into state on load because the mission list and the overview
   *  arrive independently — storing it would need an effect per arrival, each racing the
   *  operator's own click. A derivation cannot race anything: the moment `selected` is set, it
   *  wins, for ever. */
  const autoSelected = useMemo(() => {
    // NOTE: `untracked`, not `cards` — a card a mission already holds is that mission's
    // decision, and jumping to UNTRACKED for it would send the operator to a list it is not in.
    const needy = missions.find((m) => m.needs_you);
    if (needy) return needy.id;
    if (untracked.some((c) => c.pending_action)) return UNTRACKED_VIEW;
    if (missions.length) return missions[0].id;
    // No missions at all, but sessions exist: show them. Falling through to the invitation here
    // would tell an operator with live work that there is "nothing tracked yet" and hide the
    // work behind a rail that is a DRAWER on a phone — true in letter, useless in practice.
    return untracked.length ? UNTRACKED_VIEW : null;
  }, [missions, untracked]);
  const shown = selected ?? autoSelected;
  // Published in an effect, not during render: a ref write in the render phase is a lint error
  // and, more to the point, a render that is thrown away would still have published.
  useEffect(() => {
    shownRef.current = shown;
    // ONE COUNTER FOR VIEW AND SCOPE. Two would let a request captured under the old view and the
    // new scope compare equal on the half that happened to move.
    visitRef.current += 1;
    setVisitTk(visitRef.current);
  }, [shown, archived]);

  /** Adoption needs a real, LIVE mission. In the UNTRACKED view `shown` is the sentinel, so the
   *  target is the first mission that can actually hold work — and when there is none the
   *  control is disabled and says why.
   *
   *  `null` in the archived scope, deliberately: the server refuses every ordinary mutation on
   *  an archived mission ("unarchive it first", 409), so offering ADOPT there would advertise a
   *  control the backend will not honour.
   *
   *  **A CLOSED MISSION IS THE SAME CASE** (#896 review 20, finding 2). Reaching `done` /
   *  `failed` / `abandoned` RELEASES the roster, so adopting into one leaves an active session on
   *  a mission nobody follows through on. The store refuses it — that is where the guarantee
   *  lives — and picking the first *unarchived* row regardless of state meant the rail happily
   *  aimed the control at a mission the server was always going to reject. It aims at an eligible
   *  one instead, and offers nothing when there is none. */
  const adoptTarget = archived
    ? null
    : shown && shown !== UNTRACKED_VIEW
      ? shown
      : (missions.find((m) => !CLOSED_STATES.has(m.state))?.id ?? null);

  /** Flipping the scope drops the explicit selection so the derivation re-picks WITHIN the new
   *  scope. Without this the console kept showing the active mission it was on while the rail
   *  listed archived ones — a body and a rail describing different sets, with nothing on screen
   *  saying so. */
  const setScope = useCallback((next: boolean) => {
    // FIRST, so every response already in flight is stale before anything else changes. The ref
    // moves here rather than in the effect, because a response can resolve between this call and
    // the effect's re-run and must already be seen as belonging to the old scope.
    listGen.current += 1;
    appliedGen.current = listGen.current;
    archivedRef.current = next;
    setLoadingMore(false);
    setArchived(next);
    setSelected(null);
    setRail({ rows: [], total: 0, consumed: 0 });
    setStoreError(null);
  }, []);

  /** A mission was created from a composer (#889).
   *
   *  Selected from the CREATE RESPONSE, not after the rail refetch: the row is what the server
   *  just returned, so waiting for a list round trip would leave the operator looking at the view
   *  they started from with nothing to show that anything happened.
   *
   *  A mission created while the ARCHIVED scope is shown is not in that list, so the scope is
   *  reset through `setScope` — which bumps the list generation, so any response already in
   *  flight for the archived rail is stale before the new selection lands. Setting `archived`
   *  directly would leave that fence unbumped and let an archived page append underneath the new
   *  mission.
   *
   *  `setScope` clears the selection; ours is applied after it, and React batches both, so the
   *  final state is the new mission selected in the active scope. The rail refresh follows and
   *  simply finds the row already there — deliberately not awaited, because a slow or failing
   *  list must not swallow a mission that was created. */
  /** A mission's lifecycle changed. #889, finding 4 of #896's review.
   *
   *  Archiving a mission from the ACTIVE rail moves it out of that rail — so re-reading the list
   *  alone leaves the row gone from the rail while the console still renders that mission's body:
   *  a rail and a body describing different sets, which is precisely what `setScope` exists to
   *  prevent for the scope toggle. Unarchiving from Archived is the symmetric case.
   *
   *  So a scope-changing transition DROPS the explicit selection and lets the derivation re-pick
   *  inside the scope that is actually shown. The selection is dropped rather than followed across
   *  the scope: following it would silently flip the operator's rail to Archived because of one
   *  archive, which is a bigger surprise than landing on the next live mission.
   *
   *  **Which rail "actually shown" means is the caller's to say, not ours to assume** (#896
   *  review 10, finding 6). Archiving and unarchiving are the same event in opposite directions
   *  and the mission is in the OTHER scope afterwards either way, so "reload the scope on screen"
   *  is right for exactly one of them. From Archived, an unarchive that reloads Archived re-reads
   *  the one list the mission has just left: the rail keeps the row until the server drops it,
   *  and the operator is left in a scope where the mission they just restored does not belong.
   *
   *  A move INTO `active` therefore SWITCHES scope, through `setScope` rather than by setting
   *  `archived` — the generation bump is what makes every archived response already in flight
   *  stale, and the scope's own effect is what fetches the destination list. Archiving keeps the
   *  operator where they are (they are watching the active rail and a row left it); unarchiving
   *  moves them, because the mission they acted on is only visible there. */
  const onMissionChanged = useCallback(
    (opts?: {
      scopeCleared?: boolean;
      moved?: { id: string; to: "active" | "archived" };
      membershipChanged?: boolean;
    }) => {
      // THE MOVED ROW GOES NOW, not when the refresh says so (#896 review 11, finding 3).
      //
      // `reload()` swallows its failures and keeps the list it has — deliberately, because a
      // failed refresh must not empty a rail. But a SUCCESSFUL archive whose refresh then fails
      // left the archived row in the Active list, `autoSelected` picked it straight back up, and
      // the operator was looking at an archived mission's body under an Active rail with nothing
      // on screen saying anything had gone wrong, and no later poll to repair it.
      //
      // The server has already told us the mission left this scope. That is not a guess, so the
      // local list can act on it immediately and the re-read becomes a confirmation rather than
      // the only source of truth.
      // ON `moved` ALONE, not on `scopeCleared && moved` (#896 review 12, finding 1). The
      // second is the view-local half and is suppressed for a mission the operator has left —
      // which is exactly when this matters, because the rail is the thing they are looking at.
      //
      // …AND ONLY FROM THE RAIL IT LEFT (#896 review 14). A rail is not "the list"; there are
      // two, and `moved.to` says which one the mission is in NOW. Removing the row from whatever
      // happens to be on screen deleted it from the DESTINATION: archive A from Active, hold the
      // response, switch to Archived where a fresh list correctly installs A, then release the
      // held response — and A vanished from the one rail it belongs in, unreachable until a
      // reload. The row goes only when the rail on screen is the scope the mission LEFT.
      if (
        opts?.moved &&
        archivedRef.current !== (opts.moved.to === "archived")
      ) {
        const gone = opts.moved.id;
        // THE ROWS AND THE COUNT MOVE TOGETHER, OR NOT AT ALL (#896 review 13, finding 1).
        //
        // They were two `useState`s and the decrement was unconditional, so a removal a NEWER
        // list had already made was counted twice: hold A's archive, let a fresh list install
        // without A, release the old response — the filter removes nothing and `total` still
        // drops, so `rows.length >= total` hides LOAD MORE and the last mission is unreachable.
        //
        // One updater over one value, which is what makes them consistent by construction rather
        // than by the order two hooks happen to be declared in.
        setRail((prev) => {
          const rows = prev.rows.filter((m) => m.id !== gone);
          if (rows.length === prev.rows.length) return prev;
          return {
            ...prev,
            rows,
            total: Math.max(0, prev.total - 1),
            // …AND THE CURSOR WITH IT (#896 review 18). The row that just left was one of the
            // rows the server had already handed over, so a removal that decrements `total`
            // alone leaves `consumed` over-counting by one — and LOAD MORE, which now stops on
            // `consumed >= total`, disappears from a rail that still has a page to fetch.
            consumed: Math.max(rows.length, prev.consumed - 1),
          };
        });
      }
      if (
        opts?.scopeCleared &&
        opts.moved?.to === "active" &&
        archivedRef.current
      ) {
        // `setScope` clears the selection, bumps the generation and empties the rail, and its
        // effect fetches the active list. Reloading as well would issue a SECOND request under
        // the same new generation, from a `reload` closed over the old scope — the exact race
        // `setScope` was introduced to end (#896 review 8, finding 3).
        setScope(false);
        if (opts.membershipChanged) onMembershipChanged?.();
        return;
      }
      if (opts?.scopeCleared) setSelected(null);
      void reload();
      // THE OVERVIEW, not just the rail — finding 1 of #896's second review.
      //
      // UNTRACKED is derived from the overview CARDS, and each card carries the `mission_id` the
      // server stamped on it. So releasing a session, or closing a mission (which releases every
      // session it holds, server-side), leaves the card still stamped with the old mission: the
      // session is gone from the roster and absent from UNTRACKED at the same time, until the
      // outer poll happens to run. Reloading the mission list cannot fix that, because the list is
      // not where the ownership fact lives.
      if (opts?.membershipChanged) onMembershipChanged?.();
    },
    [reload, onMembershipChanged, setScope],
  );

  const onCreated = useCallback(
    (m: Mission, opts: { focus: boolean } = { focus: true }) => {
      // The mission EXISTS whatever happened on the client, so the rail is refreshed either way.
      // Only the focus is conditional: a completion that arrived after the operator cancelled or
      // moved on must not switch scope and selection out from under them (#896 review 2).
      if (!opts.focus) {
        void reload();
        return;
      }
      setNote(null);
      if (archived) {
        // SCOPE SWITCH ONLY — no `reload()` here, and that is finding 3 from #896's review.
        // `reload` is a callback closed over `archived`, so the instance in hand at this moment
        // still requests `archived=1`; and `setScope` has already bumped the list generation, so
        // that archived response would share the NEW generation with the active-scope effect and
        // overwrite the active rail with archived rows.
        //
        // Nothing is lost by dropping it: `setScope` changes `archived`, which is in the list
        // effect's dependencies, so the active list is fetched by the effect that owns that scope.
        // One owner per scope, rather than two requests racing under one generation.
        setScope(false);
      } else {
        void reload();
      }
      setSelected(m.id);
      setStop("THREAD");
    },
    [archived, setScope, reload],
  );

  const adopt = useCallback(
    (sessionKey: string) => {
      if (!adoptTarget) return;
      setAdopting(sessionKey);
      setNote(null);
      // WHICH VISIT THE OPERATOR PRESSED IT FROM (#896 review 9 finding 4; review 10 finding 3).
      //
      // The refusal is a fact about THIS attempt — "already held by mission X" — and the console
      // note is a surface that outlives the view it was raised in. Started in UNTRACKED and
      // resolved after the operator selected mission B, an unfenced error appeared over B with
      // nothing to say which mission it was about.
      //
      // A VISIT, not the id it had: `from !== shownRef.current` was satisfied again the moment
      // the operator came back, so UNTRACKED → B → UNTRACKED admitted a refusal raised two views
      // ago into a list that has since been re-read. The token cannot be re-entered.
      const from = visit();
      const attempt = ++adoptAttempt.current;
      api
        .adoptMissionSession(adoptTarget, sessionKey)
        .then(() => {
          // Optimistic, then authoritative. The server has confirmed the adoption, so the row
          // leaves UNTRACKED on the spot rather than after a poll; the overview refetch then
          // replaces the guess with the server's own stamp.
          setHeldExtra((prev) =>
            prev.includes(sessionKey) ? prev : [...prev, sessionKey],
          );
          onMembershipChanged?.();
          return reload();
        })
        .catch((e: unknown) => {
          // AUTHORITATIVE FIRST, AND UNCONDITIONALLY (#896 review 10, finding 5). A refusal is
          // not "nothing happened": the 409 this path exists to report says the session is held
          // by a mission the displayed card claims nothing about, so the picture that produced
          // the attempt is the stale one. Refreshing only on success leaves UNTRACKED asserting
          // an ownership the server has just denied, until the outer poll happens to run.
          //
          // Outside the attempt fence deliberately — a re-read is a fact about the SERVER, and
          // it is correct for whoever is looking. Only the NOTE below is view-local.
          onMembershipChanged?.();
          // Exclusive membership: a session already held comes back 409 NAMING the holder, and
          // that detail is the useful half — "no" without "where it went" is not an answer. It
          // is only an answer for the visit it was asked from, though.
          if (attempt !== adoptAttempt.current || !isVisitCurrent(from)) return;
          setNote(
            e instanceof Error
              ? e.message
              : "That session could not be adopted.",
          );
        })
        .finally(() => {
          // The busy flag is owned by the attempt, for the reason the pagination token is: a
          // superseded request clearing the current one's spinner re-enables a control that is
          // still working.
          if (attempt === adoptAttempt.current) setAdopting(null);
        });
    },
    [adoptTarget, reload, onMembershipChanged, visit, isVisitCurrent],
  );

  const onTurns = useCallback(
    (missionId: string, fn: (prev: AskTurn[]) => AskTurn[]) =>
      setTurns((prev) => ({ ...prev, [missionId]: fn(prev[missionId] ?? []) })),
    [],
  );

  const select = useCallback((id: string) => {
    setSelected(id);
    setStop("THREAD");
    setDrawerOpen(false);
    setNote(null);
    setTitle(null);
    setHeldExtra([]);
  }, []);

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  /** A decision settled here must also settle everywhere else it is drawn. The route owns the
   *  overview, so the console asks it to refetch rather than keeping a second copy. */
  const onResolved = useCallback(
    (a: OrchestratorAction) => onActionResolved?.(a),
    [onActionResolved],
  );

  const rail = (
    <MissionRail
      missions={missions}
      untracked={untracked}
      selectedId={shown}
      onSelect={select}
      storeError={storeError}
      total={total}
      hasMore={hasMore}
      needsReRead={needsReRead}
      onReRead={reload}
      loadingMore={loadingMore}
      onLoadMore={loadMoreMissions}
      archived={archived}
      onScope={setScope}
    />
  );

  return (
    <div className={styles.console} data-testid="mission-console">
      <div className={styles.railInline}>{rail}</div>
      <MissionDrawer
        open={drawerOpen}
        onClose={closeDrawer}
        triggerRef={drawerBtnRef}
      >
        {rail}
      </MissionDrawer>

      <div className={styles.centre}>
        <div className={styles.topbar}>
          <button
            type="button"
            className={styles.drawerBtn}
            onClick={() => setDrawerOpen(true)}
            ref={drawerBtnRef}
            aria-label="Missions"
            data-testid="rail-drawer-open"
          >
            ☰
          </button>
          <span className={styles.missionTitle} data-testid="console-title">
            {title ??
              (missions.length ? "Select a mission" : "MISSION CONTROL")}
          </span>
        </div>

        <div className={styles.stops} role="tablist" aria-label="Mission view">
          {STOPS.map((s) => (
            <button
              key={s}
              type="button"
              role="tab"
              aria-selected={stop === s}
              className={`${styles.stop} ${stop === s ? styles.stopOn : ""}`}
              onClick={() => setStop(s)}
              data-testid={`stop-${s.toLowerCase()}`}
            >
              {s}
            </button>
          ))}
        </div>

        {note ? (
          <div
            className={styles.notice}
            role="status"
            data-testid="console-note"
          >
            {note}
          </div>
        ) : null}

        {/* The store outage is announced HERE as well as in the rail. On a phone the rail is a
            drawer, so a rail-only notice tells the operator nothing at all — a degraded console
            that looks healthy is worse than one that says it is degraded. */}
        {storeError ? (
          <div
            className={styles.notice}
            role="status"
            data-testid="console-store-error"
          >
            <div className={styles.noticeLead}>
              The mission store could not be read.
            </div>
            <div>{storeError}. Sessions below are unaffected.</div>
          </div>
        ) : null}

        {shown === UNTRACKED_VIEW ? (
          <div className={styles.pane} data-testid="pane">
            <div className={styles.empty}>
              <div className={styles.emptyLead}>
                {untracked.length} live{" "}
                {untracked.length === 1 ? "session" : "sessions"} with no
                mission.
              </div>
              <div>
                ADOPT one into a mission from the rail, or act on what it is
                waiting for here.
              </div>
            </div>
            {/* The sessions themselves, not only their decisions.
                On a phone the rail IS a drawer, so anything that lives only on a rail row is
                reachable only by opening it — including what the orchestrator last did here.
                This view is the surface that is visible without the drawer, so it carries the
                session's own line as well as its decision.

                A decision renders through the SAME `ActionRow`, so its controls come from the
                server's projection. Before this phase it rode a card; without somewhere to go it
                would simply vanish, and a console that loses decisions for unorganised work is
                worse than the grid it replaced. */}
            <ul className={styles.sessionList} aria-label="Untracked sessions">
              {untracked.map((c) => (
                <li
                  key={c.id}
                  className={`${styles.event} ${styles.sessionBlock}`}
                  data-testid="untracked-session"
                >
                  {/* The HUD corner brackets the grid's cards carried (#476). This block is what
                    replaced those cards, so it keeps the treatment rather than quietly dropping
                    the repo's card vocabulary along with the grid. */}
                  <HudFrame />
                  <div className={styles.eventHead}>
                    {/* Colour alone is not an accessible state: the band rides the LED's
                      accessible name, exactly as the grid moved it there when it dropped the
                      section headings (#750). */}
                    <span
                      className={`${styles.dot} ${
                        c.state === "needs_you"
                          ? styles.dotNeedsYou
                          : c.live
                            ? styles.dotRunning
                            : ""
                      }`}
                      role="img"
                      aria-label={BAND_LABEL[c.state] ?? c.state}
                    />{" "}
                    {c.engine} · {c.project?.name || "no project"}
                  </div>
                  <div className={styles.eventText}>{c.title || c.id}</div>
                  {/* The review's two statements, both standing down when the action speaks. The
                    ⚠ carries the reason on its accessible name as well as in text — colour and
                    a glyph are not a reason. */}
                  {!actionSpeaks(c) &&
                  c.intervention_required &&
                  c.intervention_reason ? (
                    <div className={styles.objStale} data-testid="intervention">
                      <span
                        role="img"
                        aria-label={`Intervention required: ${c.intervention_reason}`}
                      >
                        ⚠
                      </span>{" "}
                      {c.intervention_reason}
                    </div>
                  ) : null}
                  {!actionSpeaks(c) && (c.synthesis || c.ai_summary) ? (
                    <div className={styles.objReason}>
                      {c.synthesis || c.ai_summary}
                    </div>
                  ) : null}
                  {/* The card's own jump link. Without it the only route into an untracked session
                    is the sidebar, which is exactly the "the decision is here, its session is
                    somewhere else" split this phase exists to close. */}
                  {/* "Jump into …", never "Open session": the app shell's own nav already carries
                    an "Open session overview" link, and a second control with that name is the
                    duplicate `pulse-card-once` guards against. */}
                  <Link
                    className={styles.openSession}
                    to={sessionRoute(c.id)}
                    aria-label={`Jump into ${c.title || c.id}`}
                  >
                    Jump in
                  </Link>{" "}
                  {/* ADOPT lives with the session, not on a rail row — the rail navigates. It is
                    rendered DISABLED rather than hidden when no mission is selected, so the
                    operator can see that adoption exists and why it is unavailable. */}
                  {/* `mission_id === undefined` means the server could not READ the membership
                    store — not that nobody holds this session (that is `null`). The card still
                    shows, because hiding the operator's work is the worse failure, but ADOPT is
                    refused rather than offered: a mutation the backend cannot authorize must
                    not be advertised. */}
                  <button
                    type="button"
                    className={styles.adoptInline}
                    onClick={() => adopt(c.id)}
                    disabled={
                      !adoptTarget ||
                      adopting === c.id ||
                      c.mission_id === undefined
                    }
                    title={
                      c.mission_id === undefined
                        ? "Mission membership could not be read, so adoption is unavailable"
                        : archived
                          ? "Archived missions cannot take new sessions — unarchive it first"
                          : adoptTarget
                            ? "Take this session into the selected mission"
                            : missions.length
                              ? "Every mission here is closed — reopen one, or start a new mission, before adopting a session"
                              : "Select a mission first"
                    }
                    data-testid="rail-adopt"
                  >
                    {adopting === c.id ? "…" : "ADOPT"}
                  </button>
                  {c.pending_action ? (
                    /* `embedded` — this block already names the session and links to it, which is
                     exactly what the prop means. Without it `ActionRow` renders its own session
                     link and the block carries two links to the same place (#781: one link).
                     The MISSION thread is the opposite case and is deliberately NOT embedded: a
                     mission can hold several sessions, so a decision there must name its own. */
                    <ActionRow
                      action={c.pending_action as OrchestratorAction}
                      onResolved={onResolved}
                      /* FENCED, like every other late outcome (#896 review 5, finding 4). This
                         path is not inside the keyed mission body, so nothing unmounts it when
                         the operator selects a mission — and `ActionRow`'s settled-record 409
                         calls the parent note, so an approval started here would paint its
                         refusal over whatever the operator moved to. `UNTRACKED_VIEW` is the id
                         this view holds, which is exactly what `isCurrent` compares against. */
                      onNote={noteIfUntracked}
                      embedded
                    />
                  ) : c.last_action ? (
                    <div
                      className={styles.objReason}
                      data-testid="untracked-view-last-action"
                    >
                      {c.last_action.verb.toUpperCase()}{" "}
                      {actionOutcome(c.last_action.state)}
                      {c.last_action.repeats && c.last_action.repeats > 1
                        ? ` ×${c.last_action.repeats}`
                        : ""}
                    </div>
                  ) : null}
                  {/* The action's state, folded into the block's own footer and appearing exactly
                    once (#781). The embedded row deliberately draws no frame and no state of its
                    own — one box, one footer — so the container is where this belongs. */}
                  {c.pending_action ? (
                    <div className={styles.objWhen} data-testid="session-state">
                      {c.pending_action.state}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
            {/* Ask lives here too. Until #871 the composer is not mission-qualified — it asks
                about your past work — and gating it behind "create a mission first" would make
                it unreachable on a fresh install, which the Ask box never was. Keyed on the
                sentinel so its turns are filed under this view rather than a mission. */}
            <Composer
              missionId={UNTRACKED_VIEW}
              configured={configured}
              turns={turns[UNTRACKED_VIEW] ?? []}
              onTurns={onTurns}
              visit={visit}
              isVisitCurrent={isVisitCurrent}
              onCreated={onCreated}
            />
          </div>
        ) : shown ? (
          <MissionBody
            key={shown}
            missionId={shown}
            stop={stop}
            configured={configured}
            onTitle={setTitle}
            onSessions={setHeldExtra}
            cards={allCards}
            onResolved={onResolved}
            onNote={setNote}
            isCurrent={isCurrent}
            onMissionChanged={onMissionChanged}
          />
        ) : (
          <div className={styles.pane} data-testid="pane">
            {filtered ? (
              <div
                className={styles.empty}
                data-testid="console-filtered-empty"
              >
                <div className={styles.emptyLead}>
                  No sessions match these filters
                </div>
                <div>
                  There is work here, just not in this combination — the chips
                  above are narrowing it.
                </div>
                <button
                  type="button"
                  className={styles.adoptInline}
                  onClick={onClearFilters}
                >
                  Show all sessions
                </button>
              </div>
            ) : (
              <div className={styles.empty} data-testid="console-empty">
                <div className={styles.emptyLead}>Nothing tracked yet.</div>
                <div>
                  A mission groups the sessions working one outcome, with the
                  objectives that define done.
                </div>
                {loading ? (
                  <div style={{ marginTop: 10 }}>
                    Looking for live sessions…
                  </div>
                ) : untracked.length ? (
                  <div style={{ marginTop: 10 }}>
                    {untracked.length} live{" "}
                    {untracked.length === 1 ? "session is" : "sessions are"}{" "}
                    still listed under UNTRACKED.
                  </div>
                ) : null}
              </div>
            )}
            <Composer
              missionId={UNTRACKED_VIEW}
              configured={configured}
              turns={turns[UNTRACKED_VIEW] ?? []}
              onTurns={onTurns}
              visit={visit}
              isVisitCurrent={isVisitCurrent}
              onCreated={onCreated}
            />
          </div>
        )}
      </div>
    </div>
  );
}
