/** Mission workspace (#944): searchable rail, one start action and Context-first
 * disclosures. List and detail requests retain their generation/mission fences;
 * changing a view never changes a session’s ownership or terminal lifetime. */
import { ChevronRight } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { useSectionState } from "../../app/sectionState";
import { MissionFilters, type MissionFiltersValue } from "./MissionFilters";
import { MissionLanding } from "./MissionLanding";
import { MissionDetails } from "./MissionDetails";
import { MissionTitle } from "./MissionTitle";
import { MissionSupervisorNotices } from "./MissionSupervisorBoard";
import { useMissionRailSlot } from "./railSlot";

import { ApiError, api } from "../../lib/api";
import type {
  Mission,
  MissionEvent,
  MissionListRow,
  OrchestratorAction,
  PulseAskMatch,
  PulseCard,
} from "../../types/api";

import { Link, useSearchParams } from "react-router-dom";

import { ActionRow } from "./ActionRow";
import { Composer, type AskTurn } from "./Composer";
import { MissionComposer } from "./MissionComposer";
import { MissionQuestionCard } from "./MissionQuestionCard";
import { MissionPlanCard } from "./MissionPlanCard";
import { MissionHeaderActions } from "./MissionHeaderActions";
import { useMissionStart } from "./useMissionStart";
import { type ObjectiveOp } from "./MissionObjectives";
import { MissionRail } from "./MissionRail";
import { ContextPane, ObjectivesPane, TimelinePane } from "./MissionDetail";
import { useMissionDetail } from "./useMissionDetail";
import styles from "./mission.module.css";

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
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

/** The server's mission id shape (`missions.MISSION_ID_RE`). A deep link that does not match it
 *  is never used to select, so it can never become a request. */
const MISSION_ID_RE = /^msn_[0-9a-f]{32}$/;

/** What the console is showing when no mission is selected: the new-mission page (#948). Its Ask
 *  turns are filed under this id, since there is no mission to keep them in. A real mission id is
 *  `msn_…`, so it cannot collide. */
const LANDING_VIEW = "__landing__";


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
        : event.kind === "plan_edit"
          ? "plan edited"
          : event.kind;
  // A `plan_edit` carries NO text by design (#967): it records which fields changed, never the brief.
  // Until #967 P4 draws it as a compact row, the generic row names those fields, read as strings
  // only. The meta object itself is never rendered.
  const changed =
    event.kind === "plan_edit" &&
    Array.isArray((event.meta as { changed?: unknown } | null)?.changed)
      ? ((event.meta as { changed: unknown[] }).changed.filter(
          (f): f is string => typeof f === "string",
        ) as string[])
      : [];
  const text =
    event.text ?? (changed.length ? `Changed: ${changed.join(", ")}` : "");
  return (
    <div className={styles.event} data-testid="thread-event">
      <div className={styles.eventHead}>{label}</div>
      <div className={styles.eventText}>{text}</div>
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
  detailsOpen,
  onDetails,
  onConversation,
  configured,
  onTitle,
  cards,
  onResolved,
  onNote,
  isCurrent,
  onMissionChanged,
  lifecycleSlot,
}: {
  missionId: string;
  /** Below 1400px the details sit behind ONE disclosure (#948) — whether it is open. At 1400px and
   *  above they are always beside the thread and this changes nothing. */
  detailsOpen: boolean;
  /** Open the details disclosure. */
  onDetails: () => void;
  /** Close it, giving the thread the room back. */
  onConversation: () => void;
  configured: boolean;
  onTitle: (t: string | null) => void;
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
  /** Where the mission's lifecycle bar renders (#942). The console owns the header row — one row
   *  carrying the title, the state and the actions — but only this component has the mission
   *  detail those actions act on. So the header supplies the destination and the body fills it,
   *  the same shape the rail already uses for the shell's sidebar. `null` renders in place, which
   *  keeps this component standalone in tests. */
  lifecycleSlot?: HTMLElement | null;
  onMissionChanged: (opts?: {
    /** Set only when THIS mount is still the one on screen — it clears the SELECTION. */
    scopeCleared?: boolean;
    /** WHICH mission moved and WHERE IT WENT, as one value. Global: never mount-fenced. */
    moved?: { id: string; to: "active" | "archived" };
    membershipChanged?: boolean;
  }) => void;
}) {
  const d = useMissionDetail(missionId);
  const [revealObjectives, setRevealObjectives] = useState(0);
  const showObjectives = () => {
    onDetails();
    setRevealObjectives((n) => n + 1);
  };

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
      showNotices={false}
      objectives={d.objectives}
      objectivesState={d.mission?.objectives_state}
      objectivesFailed={d.objectivesFailed}
      supervisor={d.mission?.supervisor}
      onOps={editable ? onOps : undefined}
      onStandDown={editable ? onStandDown : undefined}
      busy={mutating}
    />
  );
  /** CONTEXT IS ITS OWN TAB (#942). It used to be the third heading inside OBJECTIVES; the props
   *  are unchanged, only the pane they arrive on. */
  const context = (
    <ContextPane
      context={d.context}
      loading={!d.context}
      onMembershipChanged={d.reloadContext}
      onDetach={editable ? onDetach : undefined}
      busy={mutating}
      // SPAWN IS OFFERED ONLY WHERE IT COULD LAND (#894): a mission actually `running`, with an
      // engine to reuse and a directory to run in. Everything else — planning, a launch already
      // in flight, a closed record — would 409, and a control that can only fail is worse than no
      // control. The server is still the arbiter; this decides what to SHOW, never what is
      // allowed.
      //
      // GATED ON THE SERVER'S DERIVED FIELDS, not on `mission.engine` (review 1, finding 3). That
      // field is empty after an ordinary create -> plan -> dispatch — the create route stores no
      // engine, the claim moves it to the dispatch row and deletes the plan, and settlement
      // deletes the dispatch row — so the control was absent in the one flow it exists for, and
      // only the browser fixtures' injected `engine` hid it. `spawn_engine` is derived from the
      // session the mission is actually holding and re-checked against the capability allowlist;
      // `spawn_cwd` is the directory the panel must show and assert back.
      spawn={
        editable &&
        // `dispatching` KEEPS THE CONTROL MOUNTED (review 2, finding 5). A real claim moves the
        // mission through `dispatching` for the length of the launch, and the periodic detail
        // poll sees it. Gating on `running` alone therefore UNMOUNTED the panel mid-spawn and
        // took the operator's typed brief with it — then a refusal restored `running` and
        // remounted an empty editor. The refusal browser test missed it because its GET fixture
        // stayed `running` throughout, so the transition never happened.
        //
        // The mission cannot be in `dispatching` without something in flight, so this widens what
        // stays on screen, never what may be started: the server is still the only thing that
        // admits a spawn, and `busy` disables the button while a launch is running.
        (d.mission?.state === "running" ||
          d.mission?.state === "dispatching") &&
        d.mission?.spawn_engine &&
        d.mission?.spawn_cwd
          ? {
              engine: String(d.mission.spawn_engine),
              cwd: String(d.mission.spawn_cwd),
              cap: d.mission.spawn_cap ?? 0,
              // THE SERVER'S OWN COUNT, on the server's own definition (review 1, finding 5).
              // Counting `role === "sub"` here while the claim counted every non-null
              // `spawned_by` — which includes the primary's literal `"dispatch"` — made the two
              // disagree by exactly one: the UI said "1 of 2 used" while the claim refused the
              // second child, and at cap 1 a normally dispatched mission could never spawn at
              // all. One definition, published by the thing that enforces it.
              // `?? 0` WAS A CLAIM THE SERVER NEVER MADE (review 5, carry-forward). The field is
              // omitted when the count could not be determined, and defaulting it to zero told
              // the operator every slot was free. It travels as `null` and the control renders
              // the difference.
              live: d.mission.spawn_live ?? null,
              onChanged: (opts) => {
                d.reload();
                changedIfCurrent(opts);
              },
              onNote: noteIfCurrent,
            }
          : undefined
      }
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

  /** BOTH HALVES, like every other mutation on this screen (#904 review 18, finding 2). `d.reload`
   *  re-reads the mission DETAIL and nothing else, so planning left the rail row saying `draft` next
   *  to a pane showing the plan, and a dispatch left the session it had just attached sitting in
   *  UNTRACKED until the next supervisor poll. */
  const onLifecycleChanged = (opts?: {
    movedTo?: "active" | "archived";
    membershipChanged?: boolean;
  }) => {
    d.reload();
    changedIfCurrent(opts);
  };

  /** THE START MODEL, owned here so the header and the plan card read ONE copy (#967). Begin and
   *  Plan again are drawn by the header; the reason, the armed launch and the brief that decides it
   *  are drawn by the card. Both used to live in the card, which portalled its buttons into the
   *  header beside the lifecycle's — two owners for one row.
   *
   *  `onNote` goes THROUGH THE MOUNT FENCE, like every other async consumer (#904 review 17,
   *  finding 2). A dispatch is the LONGEST await on this screen — it launches a process — and the
   *  operator is free to move to another mission while it runs; the raw callback rendered mission
   *  A's failure on mission B's pane. */
  const start = useMissionStart(d.mission ?? null, {
    onChanged: onLifecycleChanged,
    onNote: noteIfCurrent,
    onPlan: onConversation,
    onObjectives: showObjectives,
  });

  const header = d.mission ? (
    <MissionHeaderActions
      mission={d.mission}
      start={start}
      onChanged={onLifecycleChanged}
      onNote={noteIfCurrent}
    />
  ) : null;

  /** THE COMPOSER, DOCKED (#942, #890).
   *
   *  It sends DURABLE turns: the transcript above IS the mission timeline — the route writes the
   *  operator's message in its claim transaction and the answer in its settlement — so the
   *  composer owns only the draft and the turn in flight. None of that changes here.
   *
   *  What changes is where it sits. It used to be the last child of the scrolling pane, on the
   *  THREAD stop only, which put it immediately under the final event with the rest of the column
   *  empty beneath it. Docked, it is on the bottom edge at every height — and it stays mounted
   *  across stops, so a half-typed message survives a glance at OBJECTIVES instead of being
   *  discarded by an unmount the operator did not ask for.
   */
  const composerDock = (
    <div className={styles.composerDock}>
      <MissionComposer
        missionId={missionId}
        configured={configured}
        isCurrent={isCurrent}
        onSettled={d.reload}
        detail={d.mission ?? null}
      />
    </div>
  );

  /** One line saying what is behind the disclosure, so a closed band still tells the operator
   *  whether anything there needs them. */
  const detailsId = useId();
  const objectivesSummary = d.objectivesFailed
    ? "objectives unavailable"
    : d.objectives.length
      ? `${d.objectives.filter((o) => o.state === "met").length}/${d.objectives.length} objectives met`
      : "no objectives yet";
  const detailsSummary = `${objectivesSummary} · ${keys.length} ${keys.length === 1 ? "session" : "sessions"} · ${d.events.length} events`;

  return (
    <>
      {/* ONE DISCLOSURE, NOT A TAB (#948). Below 1400px the details used to be the other half of a
          Conversation / Details tab pair, which read as a second way to pick a mission when the
          rail already does that. The thread is always on screen now; the details open in a band
          above it, and neither the thread nor its composer unmounts or moves parent when the band
          toggles (#930's draft-survival rule). At 1400px and above the band is hidden and the
          details sit beside the thread as before. */}
      <button
        type="button"
        className={styles.detailsToggle}
        aria-expanded={detailsOpen}
        aria-controls={detailsId}
        onClick={detailsOpen ? onConversation : onDetails}
        data-testid="details-toggle"
      >
        <ChevronRight size={14} aria-hidden="true" />
        <span>Details</span>
        <small>{detailsSummary}</small>
      </button>
      {lifecycleSlot && header ? createPortal(header, lifecycleSlot) : null}
      {/* THE THREAD COLUMN — a scrolling pane with the composer docked under it (#942).
          The composer used to be the last child of the scrolling pane, so it sat immediately
          after the final event and everything below it was empty: on a 1600×950 desktop with a
          quiet mission that is roughly 60% of the column, which is what the operator reported as
          "a mess". A chat fills its column and pins its input to the bottom edge. Splitting the
          two is the whole fix — the pane keeps `flex: 1` and scrolls, the dock is `flex: none`
          and sits on the bottom edge, so the empty space becomes thread instead of void. */}
      <div
        className={styles.threadCol}
      >
        <div className={styles.pane} data-testid="pane">
          {/* WHAT THE MISSION IS WAITING ON, at the TOP of the body (#967 P2b review). The no-AI
            notice, the open question and the plan card are not thread entries; they describe the
            mission the thread is about. They used to sit inside the bottom-anchored box below, so
            the whole group rode its `margin-top: auto` down to the composer. P1's card was tall
            enough to fill the pane and hide that; folded to one line, the card floated hundreds of
            pixels under the Details band. Outside that box they stay at the top, and a short
            thread's free space falls between them and the conversation instead. */}
          <div className={styles.paneTop}>
            {/* ONE HEADER ROW (#942). The mission's controls live in the console's header now, beside
            the title and the state, instead of on a second row of their own inside the pane —
            two stacked headers before any content was one of the things that made the page read
            as two designs. They are still on every stop: closing a mission from the timeline is
            as reasonable as closing it from the thread.

            Rendered in place when no slot is offered, which is what keeps this component
            standalone in a unit test. */}
            {d.mission && !lifecycleSlot ? header : null}
            {!configured ? (
              <div className={styles.notice} data-testid="no-ai-notice">
                <div className={styles.noticeLead}>
                  No AI endpoint configured.
                </div>
                <div>
                  Off: suggestions, recaps, progress, completion proposals,
                  and the composer. Still works: create, adopt, objectives,
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
            {/* The card reads the body's start model (`start`, above), whose callbacks carry
                both halves of a change and the mount fence. */}
            {d.mission ? (
              <MissionPlanCard mission={d.mission} start={start} />
            ) : null}
          </div>
          {/* THE THREAD, in its own box so a SHORT thread sits at the BOTTOM (#942). Pinning the
            composer fixed half the dead band and moved the other half: the events stayed
            top-aligned, so a four-turn mission on a 950px screen put ~350px of void between the
            last answer and the box you type into. Chats grow up from the composer.
            `.paneAtBottom` carries `margin-top: auto` — see the note beside `.pane`. Only the
            conversation rides it: decisions, events and the empty state. */}
          <div
            className={`${styles.paneInner} ${styles.paneAtBottom}`}
          >
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
          </div>
        </div>
        {composerDock}
      </div>
      <div
        id={detailsId}
        className={`${styles.detailsWrap} ${detailsOpen ? "" : styles.hideDetails}`}
      >
        <MissionDetails
          missionId={missionId}
          context={context}
          objectives={objectives}
          followThrough={
            <MissionSupervisorNotices supervisor={d.mission?.supervisor} />
          }
          timeline={timeline}
          revealObjectives={revealObjectives}
          summaries={{
            context: d.context?.cwd ? "Folder & sessions" : "Unavailable",
            objectives: d.objectivesFailed
              ? "Unavailable"
              : d.mission?.objectives_state === "pending"
                ? "Preparing…"
                : d.objectives.length
                  ? `${d.objectives.filter((o) => o.state === "met").length} / ${d.objectives.length}`
                  : "Unmeasured",
            followThrough: d.mission?.supervisor
              ? d.mission.supervisor.objectives.length
                ? `${d.mission.supervisor.unmet_gates} unmet gates`
                : "Unmeasured"
              : "Unavailable",
            timeline: `${d.events.length} events`,
          }}
        />
      </div>
    </>
  );
}

export function MissionConsole({
  allCards,
  configured,
  onActionResolved,
  onMembershipChanged,
}: {
  /** Every card from the overview. A mission's decisions are read from here — the pending actions
   *  of the sessions it holds. */
  allCards: PulseCard[];
  /** Told when a decision settles, so the route can refresh the overview the cards came from. */
  onActionResolved?: (a: OrchestratorAction) => void;
  /** Refetch the overview. Closing, archiving or detaching changes WHICH MISSION HOLDS a session,
   *  and that fact is stamped on the cards by the server. */
  onMembershipChanged?: () => void;
  /** Whether an AI endpoint is configured. The Ask composer is disabled without one. */
  configured: boolean;
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
  const [filters, saveFilters] = useSectionState<MissionFiltersValue>(
    "missions.filters",
    { q: "", project: "", state: "" },
  );
  const filtersRef = useRef(filters);
  const missionFiltered = Boolean(
    filters.q || filters.project || filters.state,
  );
  const [facets, setFacets] = useState<{
    projects: string[];
    states: string[];
  }>({ projects: [], states: [] });
  const [projectNames, setProjectNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let live = true;
    void api
      .projectEntities({ includeArchived: true })
      .then((r) => {
        if (!live) return;
        const projects = r.projects ?? [];
        setProjectNames(
          Object.fromEntries(
            projects.map((p) => [
              p.id,
              projects.filter((x) => x.name === p.name).length > 1
                ? `${p.name} · ${p.folders[0] ?? "project"}`
                : p.name,
            ]),
          ),
        );
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  const [storeError, setStoreError] = useState<string | null>(null);
  /** Has a mission-list read SUCCEEDED for the scope on screen? (#929)
   *
   *  `list.rows` is `[]` both before the first read and after a read that found nothing, so the
   *  array alone cannot tell "no missions" from "no answer yet". The first-run treatment below
   *  is gated on this, deliberately: showing "start your first mission" because the store could
   *  not be read would be the same absence-read-as-evidence mistake the mission work has already
   *  paid for twice. Set only in `applyList`, which runs on a fenced success. */
  const [listLoaded, setListLoaded] = useState(false);
  /** WHICH MISSION IS OPEN — plain state, deliberately NOT retained for the visit (#948).
   *
   *  Entering the section opens the new-mission page; that is what the operator asked the front
   *  door to be. It used to restore the last selection (#944), and before that to AUTO-SELECT a
   *  mission — the one needing you, else an untracked decision, else the first row. Both are gone.
   *  What needs the operator is previewed on the landing instead, from the same rows the rail's
   *  dots read. Filters and scope ARE still retained for the visit. */
  const [selected, setSelected] = useState<string | null>(null);
  /** The details disclosure below 1400px — kept for the visit, like the sections inside it. */
  const [detailsOpen, setDetailsOpen] = useSectionState<boolean>(
    "missions.detailsOpen",
    false,
  );

  /** `?m=<mission id>` — the one deep link into a specific mission (#948). The session header,
   *  the row menu and the bell link here.
   *
   *  It is SHAPE-CHECKED against the server's id format (`missions.MISSION_ID_RE`) before it can
   *  select anything, so a malformed value costs no request. The selection is applied during
   *  render, the documented way to adjust state to a changed input, and remembered per link
   *  value so it lands once. The URL then settles back on `/mission`, which keeps a single owner
   *  for selection — this console — rather than a query string that can disagree with it. */
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLink = searchParams.get("m");
  const [consumedLink, setConsumedLink] = useState<string | null>(null);
  if (deepLink !== consumedLink) {
    setConsumedLink(deepLink);
    if (deepLink !== null && MISSION_ID_RE.test(deepLink)) {
      setSelected(deepLink);
    }
  }
  useEffect(() => {
    if (deepLink === null) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("m");
        return next;
      },
      { replace: true },
    );
  }, [deepLink, setSearchParams]);

  /** The shell's sidebar slot (#935).
   *
   *  Read through the shell's own CONTEXT rather than by looking the element up in an effect.
   *  The lookup version worked, but it set state synchronously inside an effect — a cascading
   *  render the react-hooks rule rejects, and rightly: the shell already knows whether it is
   *  offering the slot and can simply say so. `null` means "render in place", which is the
   *  honest fallback rather than a blank column. */
  const railSlot = useMissionRailSlot();
  const { el: railSlotEl, dismiss: dismissRail } = railSlot;

  /** The composer's MODE, owned here rather than in the composer (#935, reshaped in #937
   *  review 1) — see `Composer.creating`. The draft text, the pending send and the turn history
   *  stay in the composer; only "which mode" needs to outlive a branch switch, because
   *  "+ New mission" in the rail switches a composer that has not mounted yet.
   *
   *  `startNewMission` dismisses whatever surface the rail lives in, so it closes over
   *  `dismissRail` — which is why the slot is read ABOVE this. Reading a `const` declared below
   *  it is a TDZ error, which the react-hooks lint catches and a browser would too. */
  const [composerCreating, setComposerCreating] = useState(true);
  /** Bumped by "+ New mission" so the brief takes focus — which it must not do on plain arrival. */
  const [focusKey, setFocusKey] = useState(0);
  const startNewMission = useCallback(() => {
    // Back to the front door (#948): with nothing selected the workspace IS the new-mission page.
    setSelected(null);
    setComposerCreating(true);
    setFocusKey((n) => n + 1);
    // On a phone the rail IS the drawer; leaving it open hides the field the operator is about
    // to type into.
    dismissRail();
  }, [dismissRail]);

  const [note, setNote] = useState<string | null>(null);
  const [title, setTitle] = useState<string | null>(null);
  /** Ask turns for the UNTRACKED view ONLY (#890). A mission's turns are durable and live in its
   *  timeline; this view has no mission to keep them in, so its Ask stays transient and says so
   *  on screen. Keyed by view rather than kept bare, because the sentinel is one of several
   *  things the console can be showing. */
  const [turns, setTurns] = useState<Record<string, AskTurn[]>>({});

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
  // `?? LANDING_VIEW` is load-bearing, not defensive. With nothing selected the console still
  // renders a composer — the new-mission page, where Ask must work on a fresh install — and its
  // `missionId` is the sentinel. Comparing against a null selection would make that composer
  // never current, so every answer it received would be discarded as stale: the fence firing on
  // the one surface it was never meant to guard.
  const isCurrent = useCallback(
    (id: string) => (shownRef.current ?? LANDING_VIEW) === id,
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
  const [archived, setArchived] = useSectionState("missions.archived", false);
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
        facets?: { projects: string[]; states: string[] };
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
      setListLoaded(true); // a fenced, in-scope answer arrived — see `listLoaded`
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
      if (r.facets) setFacets(r.facets);
      setStoreError(r.store_error ?? null);
    },
    [],
  );

  useEffect(() => {
    archivedRef.current = archived;
    let live = true;
    const gen = nextGen();
    api
      .missions({ ...filters, limit: PAGE, archived })
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
  }, [applyList, archived, filters, nextGen]);

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
    const query = filtersRef.current;
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
        const r = await api.missions({
          ...query,
          limit: open,
          archived: scope,
        });
        if (scope !== archivedRef.current) return;
        applyList(gen, scope, {
          facets: r.facets,
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
            ...query,
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
        facets: results[0].facets,
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
        facets: out.facets,
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
      .missions({
        ...filtersRef.current,
        limit: PAGE,
        offset: consumed,
        archived: scope,
      })
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

  const shown = selected;
  /** The header's title, from the SAME mission the body renders.
   *
   *  `title` is pushed up by the body after its detail fetch, so on its own it is null for the
   *  whole of that round trip — and the header contradicted the body for exactly that window.
   *  The rail row is already loaded and already carries the title, so it answers immediately; the
   *  fetched value still wins when it differs, which is what keeps a rename correct. */
  const shownRow = shown ? missions.find((m) => m.id === shown) : undefined;
  const shownTitle = title ?? shownRow?.title ?? null;
  /** The header's slot for the mission's lifecycle bar — see `MissionBody.lifecycleSlot`. */
  const [lifecycleSlotEl, setLifecycleSlotEl] = useState<HTMLElement | null>(
    null,
  );
  // Published in an effect, not during render: a ref write in the render phase is a lint error
  // and, more to the point, a render that is thrown away would still have published.
  useEffect(() => {
    shownRef.current = shown;
    // ONE COUNTER FOR VIEW AND SCOPE. Two would let a request captured under the old view and the
    // new scope compare equal on the half that happened to move.
    visitRef.current += 1;
  }, [shown, archived]);



  /** Flipping the scope drops the explicit selection so the derivation re-picks WITHIN the new
   *  scope. Without this the console kept showing the active mission it was on while the rail
   *  listed archived ones — a body and a rail describing different sets, with nothing on screen
   *  saying so. */
  const setScope = useCallback(
    (next: boolean) => {
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
      // …AND THE EVIDENCE THAT A LIST WAS READ, which belongs to the scope that produced it
      // (#930 review 1, finding 4). `listLoaded` is what separates "no missions" from "not asked
      // yet", so carrying the previous scope's successful read across a scope change makes
      // `firstRun` true the instant Archived → Active is clicked — inviting the operator to start
      // their first mission while their actual active list is still in flight. Cleared here, in
      // the same synchronous step as the rows, so no render ever sees rows from neither scope
      // alongside a loaded flag from the old one.
      setListLoaded(false);
    },
    [setArchived, setSelected],
  );

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
    [reload, onMembershipChanged, setScope, setSelected],
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
    },
    [archived, setScope, reload, setSelected],
  );


  const onTurns = useCallback(
    (missionId: string, fn: (prev: AskTurn[]) => AskTurn[]) =>
      setTurns((prev) => ({ ...prev, [missionId]: fn(prev[missionId] ?? []) })),
    [],
  );

  const select = useCallback(
    (id: string) => {
      setSelected(id);
      // CLOSE THE SURFACE THE RAIL LIVES IN (#940). Selection changes local state and never the
      // URL, so the shell's pathname effect cannot see it — without this the drawer stays open
      // over the mission that was just picked. `dismiss` is a no-op on a docked column, so this
      // is unconditional rather than guarded on a width the console should not know about.
      dismissRail();
      setNote(null);
      setTitle(null);
    },
    [dismissRail, setSelected],
  );

  /** A decision settled here must also settle everywhere else it is drawn. The route owns the
   *  overview, so the console asks it to refetch rather than keeping a second copy. */
  const changeFilters = useCallback(
    (next: MissionFiltersValue) => {
      filtersRef.current = next;
      listGen.current += 1;
      appliedGen.current = listGen.current;
      pageAttempt.current += 1;
      openRef.current = PAGE;
      setSelected(shownRef.current);
      saveFilters(next);
      setRail({ rows: [], total: 0, consumed: 0 });
      setLoadingMore(false);
      setStoreError(null);
      setListLoaded(false);
    },
    [saveFilters, setSelected],
  );

  const onResolved = useCallback(
    (a: OrchestratorAction) => onActionResolved?.(a),
    [onActionResolved],
  );

  const rail = (
    <MissionRail
      headEl={railSlotEl ? railSlot.headEl : null}
      footEl={railSlotEl ? railSlot.footEl : null}
      missions={missions}
      filters={
        <MissionFilters
          value={filters}
          onChange={changeFilters}
          facets={facets}
          projectNames={projectNames}
        />
      }
      projectNames={projectNames}
      loading={!listLoaded && !storeError}
      filtered={missionFiltered}
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
      onNewMission={startNewMission}
    />
  );

  /** The landing's recovery: back to the Active scope with no filters (#948). */
  const clearAll = useCallback(() => {
    changeFilters({ q: "", project: "", state: "" });
    if (archived) setScope(false);
  }, [changeFilters, archived, setScope]);

  return (
    <div className={styles.console} data-testid="mission-console">
      {/* THE RAIL RENDERS INTO THE SHELL'S SIDEBAR ON THIS ROUTE (#935).
          The operator had two vertical lists side by side — the shell's sessions and this one —
          so the shell lists missions here instead and the console keeps only the thread.

          A PORTAL, not a lift. `MissionConsole` still OWNS the list: `listGen` / `appliedGen` /
          `archivedRef` fencing, pagination, the scope switch and the selection all stay here,
          because moving them into the shell would move three review rounds of race handling with
          them. Only the DOM position changes.

          Falls back to rendering in place when the slot is absent — an older shell, or the first
          paint of a route that has not mounted it yet. A rail in the wrong column beats no rail,
          and this is also what keeps the component testable on its own. */}
      {/* ONE RAIL, IN THE SHELL, AT EVERY WIDTH (#940).
          The console used to keep its own `MissionDrawer` on a phone, because the shell's
          off-canvas sidebar had a backdrop and none of the rest of the modal contract — so
          portalling there would have removed a focus trap from the surface that most needs one.
          The shell carries that contract now, so the second drawer has no reason to exist, and
          the operator stops meeting two hamburgers on one screen.

          The in-place fallback stays for the case the slot is genuinely absent: a route that
          does not offer one, an older shell, the first paint before it mounts, and the component
          rendered on its own in a unit test. A rail in the wrong column beats no rail. */}
      {railSlotEl ? (
        createPortal(rail, railSlotEl)
      ) : (
        <div className={styles.railInline}>{rail}</div>
      )}

      <div className={styles.centre}>
        {/* THE HEADER BELONGS TO A MISSION (#948). With nothing selected the workspace is the
            new-mission page, whose own heading says what to do — a "Select a mission" row above it
            was the old empty state speaking over the new one. */}
        {shown ? (
          <div className={styles.topbar}>
            {/* THE TITLE COMES FROM THE MISSION THE BODY IS RENDERING (#942): the rail row answers
                at once, and the fetched title wins only to catch a rename. */}
            {shownTitle ? (
              <MissionTitle key={shown} title={shownTitle} />
            ) : (
              <span className={styles.missionTitle} data-testid="console-title">
                Loading mission…
              </span>
            )}
            {/* Where `MissionBody` portals the mission's state and actions, so the header is ONE
                row rather than a title above a second bar. */}
            <span
              className={styles.topbarActions}
              ref={setLifecycleSlotEl}
              data-testid="header-actions"
            />
          </div>
        ) : null}

        {shown && listLoaded && !shownRow && missionFiltered ? (
          <div className={styles.filterNotice}>
            {hasMore || needsReRead || storeError
              ? "Not in the loaded mission results"
              : "Outside current filters"}{" "}
            ·{" "}
            <button
              type="button"
              onClick={() => changeFilters({ q: "", project: "", state: "" })}
            >
              Clear mission filters
            </button>
          </div>
        ) : null}
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
            drawer, so a rail-only notice tells the operator nothing at all. */}
        {storeError ? (
          <div
            className={styles.notice}
            role="status"
            data-testid="console-store-error"
          >
            <div className={styles.noticeLead}>
              The mission store could not be read.
            </div>
            <div>{storeError}.</div>
          </div>
        ) : null}

        <div className={styles.split} data-testid="split">
          {shown ? (
            <MissionBody
              key={shown}
              missionId={shown}
              detailsOpen={detailsOpen}
              onDetails={() => setDetailsOpen(true)}
              onConversation={() => setDetailsOpen(false)}
              configured={configured}
              onTitle={setTitle}
              cards={allCards}
              onResolved={onResolved}
              onNote={setNote}
              isCurrent={isCurrent}
              lifecycleSlot={lifecycleSlotEl}
              onMissionChanged={onMissionChanged}
            />
          ) : (
            <MissionLanding
              composer={
                <Composer
                  missionId={LANDING_VIEW}
                  configured={configured}
                  turns={turns[LANDING_VIEW] ?? []}
                  onTurns={onTurns}
                  visit={visit}
                  isVisitCurrent={isVisitCurrent}
                  onCreated={onCreated}
                  creating={composerCreating}
                  onCreatingChange={setComposerCreating}
                  focusKey={focusKey}
                />
              }
              missions={missions}
              filtered={missionFiltered || archived}
              partial={hasMore || needsReRead}
              loaded={listLoaded}
              unavailable={!!storeError}
              onSelect={select}
              onClearFilters={clearAll}
              projectNames={projectNames}
            />
          )}
        </div>
      </div>
    </div>
  );
}
