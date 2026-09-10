/** One mission's data — thread events, objectives, context, timeline (#878).
 *
 * **This component is keyed on the mission id by its parent, so switching missions REMOUNTS it.**
 * That is the whole fencing strategy, and it is deliberate: the alternative is resetting five
 * pieces of state in an effect and checking a captured id inside every late `.then`, which is
 * both more code and the kind of check that is correct until someone adds a sixth fetch and
 * forgets it. A remount cannot forget. A late response from the previous mission resolves into
 * an unmounted instance and updates nothing.
 *
 * **A turn is not transient and does not live in this component.** It is a row in the store, and
 * the mission read carries it (`mission.turn`) — which is what makes "still working" and an
 * ambiguous turn survive a reload. This hook's only job around it is CADENCE: while one is open
 * the detail is re-read on a short bounded interval, because a model call takes seconds and the
 * supervisor cadence is two and a half minutes (#890, #902 review 2).
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import type {
  Mission,
  MissionContext,
  MissionEvent,
  MissionObjective,
} from "../../types/api";

const EVENTS_PAGE = 40;

/** How often the mission DETAIL is refetched while a console is open.
 *
 *  Half the supervisor's own `INTERVAL_S` (300s), so a board is at most one sweep behind rather
 *  than aliasing against it — polling at exactly the sweep period can sit permanently just before
 *  each sweep and show the previous one's state forever. */
const SUPERVISOR_POLL_MS = 150_000;

/** How often the OBJECTIVE list is re-read while its producer is still running (#889).
 *
 *  The full load runs once per mission, which is right for a list the operator changes — but
 *  `objectives_state: "pending"` is a list the SERVER is about to change, from a background task
 *  that normally finishes in seconds. On the 150s detail cadence alone, "working out what done
 *  means…" sat on screen for minutes after the objectives existed. */
const OBJECTIVES_POLL_MS = 3_000;

/** …and it is BOUNDED. A `pending` that never settles is a real state — the producer can die, and
 *  `recover_pending` re-drives it only at startup — so an unbounded poll would hammer two
 *  endpoints for the life of the page. After this many attempts the console stops asking and the
 *  ordinary detail cadence takes over, which is the honest fallback: the pane keeps saying the
 *  producer has not answered, because it has not. */
const OBJECTIVES_POLL_MAX = 40;

/** …and how often it is refetched while a TURN is open (#902 review 2, finding 4).
 *
 *  A model call takes seconds; the supervisor cadence is two and a half MINUTES. On a fresh page
 *  there is no outstanding composer request whose callback could ask again, so a reloaded
 *  `in_progress` turn sat saying "Still working" — and withheld an answer the server already
 *  had — until the next supervisor tick. That is not a stale board, it is a stale conversation.
 *
 *  Bounded for the same reason the pending-objective poll is: a turn that never settles is a real
 *  state (a worker can die mid-turn; recovery re-drives it), and an unbounded cadence would hit
 *  the endpoint for the life of the page. Past the bound the ordinary cadence still runs. */
const TURN_POLL_MS = 3_000;
const TURN_POLL_MAX = 60;

export interface MissionDetailState {
  mission: Mission | null;
  events: MissionEvent[];
  objectives: MissionObjective[];
  /** The objectives READ failed. `[]` alone cannot tell "this mission has none" from "we could
   *  not look", and since #942 folded follow-through onto these rows the difference is load-
   *  bearing: no rows means no assessment on screen. */
  objectivesFailed: boolean;
  context: MissionContext | null;
  cursor: number | null;
  loadingMore: boolean;
  loadOlder: () => void;
  /** Re-read the ROSTER now. Membership is a row another tab can change, and a control the
   *  server has just refused for that reason is the page finding out (#903 review 3, finding 1).
   *  The context is otherwise loaded once, deliberately — this is the one thing that invalidates
   *  it from underneath. */
  reloadContext: () => void;
  /** Re-read everything from the server (#889, #890).
   *
   *  Called after any operator mutation — a state transition, an objective edit, a stand-down —
   *  and, critically, **after a failed one too**: a 409 means the mission is not what this client
   *  believed, so the next render has to come from the server rather than from the state that was
   *  asked for. The composer calls it on every turn settlement for the same reason: the route
   *  writes the operator's message inside its CLAIM transaction, so even a turn that then failed
   *  has changed what the timeline says.
   *
   *  Implemented as a bump of the effect's identity rather than as a second fetch path, so it
   *  inherits the SAME `live` fence the mount load already has. A superseded response resolves
   *  into a cleaned-up effect and updates nothing; a second, hand-rolled guard would be a second
   *  thing to keep correct.
   *
   *  The DISPATCH PROPOSAL rides on the same row (#893), and planning, editing and dispatching
   *  all change it — so the plan card calls this too rather than waiting out the supervisor
   *  poll, which would leave it showing the proposal the operator has just replaced. */
  reload: () => void;
}

/** Loads one mission. Split from the panes so the panes stay pure and the loading lives in one
 *  place — the console renders the same data in two different layouts (stops vs the persistent
 *  column) and must not fetch it twice. */
export function useMissionDetail(missionId: string): MissionDetailState {
  const [mission, setMission] = useState<Mission | null>(null);
  const [events, setEvents] = useState<MissionEvent[]>([]);
  const [objectives, setObjectives] = useState<MissionObjective[]>([]);
  /** Did the objectives READ fail? (#942 review 1.)
   *
   *  `[]` used to mean two different things — "this mission has none" and "we could not look" —
   *  because the load swallowed its rejection. That was survivable while the supervisor's board
   *  rendered from its own list; once follow-through folded onto the objective rows, an empty list
   *  meant no rows, and no rows meant the assessment and its STAND DOWN controls vanished while
   *  "1 unmet gate" went on being displayed above them. The console said, in one breath, that a
   *  gate was unmet and that there was nothing to follow through on.
   *
   *  Same three-way answer the rest of this codebase insists on: none, not-yet, and unreadable. */
  const [objectivesFailed, setObjectivesFailed] = useState(false);
  const [context, setContext] = useState<MissionContext | null>(null);
  // A COUNTER, not a callback: the loader lives inside the effect (it closes over `live`, which
  // is what makes a late response harmless), so the way to re-run it is to re-run the effect.
  const [ctxNonce, setCtxNonce] = useState(0);
  const reloadContext = useCallback(() => setCtxNonce((n) => n + 1), []);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  /** Bumped by `reload`. Part of the load effect's identity, which is what makes a manual re-read
   *  take the same cleanup-fenced path as the mount load. */
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  /** The installed mission, readable from inside a resolved promise without re-identifying the
   *  effect that issued it. `installMission` needs to know whether the row it is about to write
   *  SETTLES the producer, and that is a question about what is on screen now. */
  const missionRef = useRef<Mission | null>(null);
  useEffect(() => {
    missionRef.current = mission;
  }, [mission]);

  /** ONE ORDER CONTRACT FOR EVERY OBJECTIVE WRITE (#896 review 5, finding 1).
   *
   *  There are three readers of the objective list — the mount's full load, the 3s pending poll
   *  and the settlement inside `installMission` — and giving the last two an ordering while the
   *  first kept only the effect-level `live` flag left the hole open at the other end: the full
   *  load's request can snapshot BEFORE the producer settles and resolve AFTER the poll has
   *  installed `done` plus the new list, restoring the empty one with no poll left to repair it.
   *
   *  So every write takes a ticket when its request is ISSUED and applies only if nothing newer
   *  has already landed. A generation is the only thing that can order responses that were
   *  issued by different code paths — `live` orders an effect's lifetime, which is a different
   *  question. */
  /** The live epoch and cursor, readable from inside a resolved promise. `loadOlder` has to ask
   *  "is the list I extended still the list on screen?", which is a question about NOW rather
   *  than about the render its closure was made in. */
  const nonceRef = useRef(0);
  const cursorRef = useRef<number | null>(null);
  useEffect(() => {
    nonceRef.current = nonce;
  }, [nonce]);
  useEffect(() => {
    cursorRef.current = cursor;
  }, [cursor]);

  const objIssued = useRef(0);
  const objApplied = useRef(0);
  /** THE SETTLED LIST IS STILL OWED. Set when the read paired with a settlement fails, cleared
   *  the moment one succeeds (#896 review 7, finding 1).
   *
   *  Without it a single failed GET was permanent: the settlement is installed regardless — it
   *  is the truth, and refusing to install it would leave the pane claiming the producer is
   *  still working — but `settles` is computed from the PREVIOUS row, so once `done` is in
   *  place no later tick ever asks again, and the bounded fast poll has already stopped. The
   *  pane then sits on `done` over the empty list it started with, for the life of the page. */
  /** …and it is owed FROM A TICKET, not as a bare boolean (#896 review 8, finding 1).
   *
   *  0 means no debt. Otherwise it is the first ticket that could possibly settle it: any read
   *  ISSUED after the failed one, because only such a read can have snapshotted after the
   *  producer settled. A boolean let the mount's full load clear the debt with a response whose
   *  own snapshot predated the settlement — its stale ticket was rejected by the order contract
   *  and it still marked the list as delivered, so the next tick saw `done`, no debt, and never
   *  asked again. The pane stayed settled over the empty list for the life of the page.
   *
   *  Cleared inside `putObjectives`, where a write is actually APPLIED, rather than at any call
   *  site that merely finished — those are different events and the gap between them is the bug. */
  const objOwedFrom = useRef(0);

  /** ONE ORDER CONTRACT FOR THE MISSION ROW TOO (#896 review 10, finding 2).
   *
   *  The objective writes were ticketed; the row, the events and the cursor were not — and there
   *  are two pollers writing them. A slow 150s read can snapshot `pending` and land after the
   *  fast poll has installed `done` plus newer events, restoring stale lifecycle state and a
   *  cursor pointing into a page that is no longer on screen. Two overlapping ticks of the SAME
   *  interval do it as well, because an async tick can outlive its period.
   *
   *  A ticket is taken when the request is ISSUED and compared when it is applied, exactly as
   *  the objective contract does — `live` orders an effect's lifetime, which is a different
   *  question and cannot see two responses within one lifetime. */
  const rowIssued = useRef(0);
  const rowApplied = useRef(0);
  const rowTicket = useCallback(() => (rowIssued.current += 1), []);
  useEffect(() => {
    rowIssued.current = 0;
    rowApplied.current = 0;
  }, [missionId]);
  // Per mission: a debt belongs to the list it was incurred against.
  useEffect(() => {
    objOwedFrom.current = 0;
  }, [missionId]);
  const objTicket = useCallback(() => (objIssued.current += 1), []);
  const putObjectives = useCallback(
    (ticket: number, rows: MissionObjective[]) => {
      if (ticket < objApplied.current) return;
      objApplied.current = ticket;
      setObjectives(rows);
      // A SUCCESS CLEARS THE READ FAILURE, and it is cleared HERE rather than at the call site
      // (#942 review 4). The flag first lived beside the one load path that set it, so every
      // OTHER accepted install — the pending-producer poll, the settlement recovery — applied
      // fresh rows and left "could not be read" on screen over them. Reproduced: an initial 503
      // followed by a settled `skipped` mission and a successful empty list still said the list
      // could not be read, instead of "No objectives were proposed", and nothing later repaired
      // it because the producer had stopped being pending.
      //
      // Data and read-status are one fact about one read, so they are applied together under one
      // ticket. That is also what makes a superseded failure harmless: `putObjectivesFailed`
      // below drops any ticket this applier has already passed.
      setObjectivesFailed(false);
      // Only a read issued after the failure can have seen the settled list.
      if (objOwedFrom.current && ticket >= objOwedFrom.current)
        objOwedFrom.current = 0;
    },
    [],
  );

  /** Record that an objectives read FAILED — under the same ordering as a success.
   *
   *  Without the ticket a slow rejection could land after a newer read had already succeeded and
   *  paint "could not be read" over rows that are on screen. The applier is the single place that
   *  decides which read is current, so both outcomes have to go through it. */
  const putObjectivesFailed = useCallback((ticket: number) => {
    if (ticket < objApplied.current) return;
    objApplied.current = ticket;
    setObjectivesFailed(true);
  }, []);

  /** Install a mission row — and, when this row is the one that ENDS the objective producer's
   *  wait, the list that settlement refers to, in the SAME render.
   *
   *  A settlement is not a field changing (#896 review 4, finding 1). It is the moment "working
   *  out what done means…" is replaced by a list, so the list has to be at least as new as the
   *  settlement — and the read that produces it has to survive the teardown its own install
   *  causes. Installing the settled row first flips `pending` to false, tears the poll effect
   *  down, and the paired objectives response is then discarded by that effect's own `live`
   *  flag: the pane finishes at `done` with the empty list it started with.
   *
   *  So the objectives are read and installed BEFORE the row that ends the wait. Both readers —
   *  the 3s pending poll and the 150s detail poll — go through here, because a rule that holds
   *  on one path and not the other is the same defect through the other door. */
  const installMission = useCallback(
    async (
      m: Mission,
      alive: () => boolean,
      /** The ticket this row's READ took when it was issued. A response with a lower ticket than
       *  one already applied is a superseded answer and is dropped — see `rowIssued`. */
      rowTk: number,
      paired?: MissionObjective[],
      pairedTicket?: number,
    ) => {
      if (rowTk < rowApplied.current) return;
      const settles =
        m.objectives_state !== "pending" &&
        missionRef.current?.objectives_state === "pending";
      if (paired !== undefined) {
        // The caller already read a list strictly after this row — the fast poll does, on every
        // tick — so there is nothing to fetch and everything to ORDER. It carries the ticket it
        // took when it issued that read.
        if (!alive()) return;
        // `putObjectives` clears the debt itself, and only when the write is APPLIED.
        putObjectives(pairedTicket ?? objTicket(), paired);
      } else if (settles || objOwedFrom.current) {
        // `settles` is true for exactly one row — the one that ends the wait — so a failure on
        // that row was the last chance the old code ever had. The debt turns "the last chance"
        // into "every later tick", and it is recorded as the FIRST TICKET THAT COULD SETTLE IT:
        // this read's own ticket. A response issued earlier cannot have seen the settled list,
        // so it must not be able to mark the list delivered.
        const ticket = objTicket();
        objOwedFrom.current = ticket;
        try {
          const r = await api.missionObjectives(missionId);
          if (!alive()) return;
          putObjectives(ticket, r.objectives);
        } catch {
          // The STATE is still the truth and is installed regardless; the list is what may be
          // stale, and the debt above is what makes the ordinary detail cadence re-read it.
          // Refusing to install the settlement here would leave the pane claiming the producer
          // is still working.
        }
      }
      if (!alive()) return;
      // RE-CHECKED after the await above: the objectives fetch inside `settles` is long enough
      // for a newer row to land, and applying this one afterwards would undo it.
      if (rowTk < rowApplied.current) return;
      rowApplied.current = rowTk;
      setMission(m);
      setEvents(m.events ?? []);
      setCursor(m.events_next_seq ?? null);
    },
    [missionId, objTicket, putObjectives],
  );

  useEffect(() => {
    let live = true;

    // The FULL load, including the two side panels. Runs once per mission.
    const loadAll = () => {
      const ticket = objTicket();
      api
        .missionObjectives(missionId)
        // No explicit clearing here: `putObjectives` clears the debt exactly when a QUALIFYING
        // response is applied, and this one may well not qualify — the mount's read can have
        // snapshotted before the producer settled and resolve long afterwards.
        .then((r) => live && putObjectives(ticket, r.objectives))
        .catch(() => live && putObjectivesFailed(ticket));
      api
        .missionContext(missionId)
        .then((c) => live && setContext(c))
        .catch(() => undefined);
    };

    // The mission row alone — which is what carries the supervisor reading, so this is the part
    // that has to keep moving. The supervisor mutates its state on its own 5-minute cadence:
    // a nudge is sent, a budget is spent, an objective is stood down, an episode escalates. With
    // a fetch keyed only on `missionId`, an open console kept showing READY indefinitely after
    // any of those — the board was accurate exactly once, at mount.
    //
    // Only the DETAIL is refetched, never the objectives or the context: those change when the
    // operator changes them (and the console already reloads on that path), while the supervisor
    // reading changes underneath a console nobody is touching. Refetching all three would triple
    // the poll cost to keep one of them current.
    const loadMission = () => {
      const tk = rowTicket();
      api
        .mission(missionId, { eventsLimit: EVENTS_PAGE })
        .then((m) => installMission(m, () => live, tk))
        .catch(() => undefined);
    };

    loadMission();
    loadAll();
    const t = setInterval(loadMission, SUPERVISOR_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
    // `ctxNonce` is in the identity too (#903 review 3, finding 1): the context is loaded once
    // per mission, and a refused per-session control is the one thing that invalidates it from
    // underneath.
  }, [
    missionId,
    nonce,
    ctxNonce,
    installMission,
    objTicket,
    putObjectives,
    putObjectivesFailed,
    rowTicket,
  ]);

  /** The pending-objectives poll (#889). Its own effect, keyed on the state it is waiting for, so
   *  it starts when the mission says `pending` and stops the moment it does not — rather than
   *  living inside the main load effect, where it would restart the whole mission fetch every
   *  three seconds.
   *
   *  Re-reads BOTH: the objectives (what the operator is waiting to see) and the mission row
   *  (which carries `objectives_state`, and is therefore the only thing that can end the wait).
   *  Polling only the objectives would leave the pane saying "working out what done means" over a
   *  list that had already arrived. */
  const pending = mission?.objectives_state === "pending";
  useEffect(() => {
    if (!pending) return;
    let live = true;
    let attempts = 0;
    const tick = async () => {
      if (!live) return;
      attempts += 1;
      if (attempts > OBJECTIVES_POLL_MAX) {
        clearInterval(t);
        return;
      }
      // SEQUENCED, and the order is the fix (#896 review 3, finding 1).
      //
      // Fired in parallel, the two reads take independent snapshots: the objectives request can
      // snapshot BEFORE the producer's transaction while the mission request snapshots after it.
      // The hook then sees `objectives_state: "done"`, stops polling, and keeps the empty list
      // for ever — settled, and wrong.
      //
      // Reading the mission FIRST and the objectives after its response has arrived makes the
      // objectives read start strictly later than the settlement it is reacting to, so it cannot
      // see a snapshot older than it. `installMission` owns the second read and the ORDER of the
      // two writes (#896 review 4, finding 1) — the list lands before the row that ends the wait,
      // so the response cannot be discarded by the teardown its own install triggers.
      let m: Mission;
      const rowTk = rowTicket();
      try {
        m = await api.mission(missionId, { eventsLimit: EVENTS_PAGE });
      } catch {
        return; // try again next tick; a failed read is not a settlement
      }
      if (!live) return;
      let paired: MissionObjective[] | undefined;
      const ticket = objTicket();
      try {
        paired = (await api.missionObjectives(missionId)).objectives;
      } catch {
        paired = undefined; // the list is unchanged; see below
      }
      if (!live) return;
      if (paired === undefined && m.objectives_state !== "pending") {
        // The row would END the wait and there is no list to end it with. Settling here would
        // replace "working out what done means…" with whatever this hook happens to hold, which
        // is the same defect the ordering below exists to prevent. The next tick tries again,
        // and the poll is bounded, so this cannot wait for ever.
        return;
      }
      await installMission(m, () => live, rowTk, paired, ticket);
    };
    const t = setInterval(() => void tick(), OBJECTIVES_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
    // `nonce` IS a dependency, and that is finding 2 from #896's review: without it this effect
    // survives a `reload()`, so a poll issued BEFORE an operator edit can resolve AFTER the
    // reload that fetched the edited list and put the stale objectives back. Sharing the epoch
    // means a reload tears this effect down — `live` goes false and the in-flight response
    // updates nothing — and starts a fresh one, which is the same fence the main load already
    // relies on rather than a second mechanism beside it.
  }, [missionId, pending, nonce, installMission, objTicket, rowTicket]);

  // THE OPEN-TURN CADENCE. Runs only while one is open, and stops the moment it settles — so a
  // console with nothing in flight is exactly as quiet as it was before.
  const openTurnId = mission?.turn?.turn_id ?? null;
  useEffect(() => {
    if (!openTurnId) return;
    let live = true;
    let attempts = 0;
    const tick = () => {
      if (!live) return;
      attempts += 1;
      if (attempts > TURN_POLL_MAX) {
        clearInterval(t);
        return;
      }
      // THROUGH `installMission`, WITH A TICKET — not a bare `setMission` (#896 review 10,
      // finding 2, applied to this poller too). This is the THIRD source of mission rows, and
      // the whole point of the ticket is that a slow response cannot restore state a newer one
      // replaced. A fast poller writing the row directly would be exactly the unordered write
      // the ticket exists to stop, arriving through a door the fix had not been told about.
      const tk = rowTicket();
      api
        .mission(missionId, { eventsLimit: EVENTS_PAGE })
        .then((m) => installMission(m, () => live, tk))
        .catch(() => undefined);
    };
    const t = setInterval(tick, TURN_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
    // Keyed on the turn ID, so a NEW turn restarts the budget and a settled one tears the
    // interval down. Keyed on the mission's identity would restart it on every poll response.
  }, [missionId, openTurnId, installMission, rowTicket]);

  const loadOlder = useCallback(() => {
    if (cursor == null || loadingMore) return;
    setLoadingMore(true);
    // FENCED AGAINST A RELOAD, and this is not cosmetic (#896 review 5, finding 2).
    //
    // A page is an extension of the list it was computed FROM. Show `100…61`, ask for
    // `before=60`, and let a reload install `110…71` with cursor `70` while it is in flight: the
    // old page then appends `59…20` and sets the cursor to `19`. Events `70…60` are now absent
    // from the timeline and no future cursor can ever reach them — a hole, not a duplicate, and
    // the operator has no way to tell there is one.
    //
    // So it records the epoch it was issued in and the cursor it extended, and applies only if
    // both are still current. `loadingMore` is cleared regardless, or a superseded page would
    // leave the control disabled for ever.
    const epoch = nonce;
    const base = cursor;
    api
      .mission(missionId, { eventsLimit: EVENTS_PAGE, before: cursor })
      .then((m) => {
        if (epoch !== nonceRef.current || base !== cursorRef.current) return;
        setEvents((prev) => [...prev, ...(m.events ?? [])]);
        setCursor(m.events_next_seq ?? null);
      })
      .catch(() => undefined)
      // Leaving what is already shown: a failed page is not a reason to drop the timeline.
      .finally(() => setLoadingMore(false));
  }, [missionId, cursor, loadingMore, nonce]);

  return {
    mission,
    events,
    objectives,
    objectivesFailed,
    context,
    cursor,
    loadingMore,
    loadOlder,
    reloadContext,
    reload,
  };
}
