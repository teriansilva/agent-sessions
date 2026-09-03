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
import { useCallback, useEffect, useState } from "react";

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
  context: MissionContext | null;
  cursor: number | null;
  loadingMore: boolean;
  loadOlder: () => void;
  /** Re-read everything from the server.
   *
   *  The composer calls it on every settlement, success or failure: the route writes the
   *  operator's message inside its CLAIM transaction, so even a turn that then failed has
   *  changed what the timeline says (#890).
   *
   *  Implemented as a bump of the effect's identity rather than as a second fetch path, so it
   *  inherits the SAME `live` fence the mount load already has. A superseded response resolves
   *  into a cleaned-up effect and updates nothing; a second, hand-rolled guard would be a second
   *  thing to keep correct. */
  reload: () => void;
}

/** Loads one mission. Split from the panes so the panes stay pure and the loading lives in one
 *  place — the console renders the same data in two different layouts (stops vs the persistent
 *  column) and must not fetch it twice. */
export function useMissionDetail(missionId: string): MissionDetailState {
  const [mission, setMission] = useState<Mission | null>(null);
  const [events, setEvents] = useState<MissionEvent[]>([]);
  const [objectives, setObjectives] = useState<MissionObjective[]>([]);
  const [context, setContext] = useState<MissionContext | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  /** Bumped by `reload`. Part of the load effect's identity, which is what makes a manual re-read
   *  take the same cleanup-fenced path as the mount load. */
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let live = true;

    // The FULL load, including the two side panels. Runs once per mission.
    const loadAll = () => {
      api
        .missionObjectives(missionId)
        .then((r) => live && setObjectives(r.objectives))
        .catch(() => undefined);
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
      api
        .mission(missionId, { eventsLimit: EVENTS_PAGE })
        .then((m) => {
          if (!live) return;
          setMission(m);
          setEvents(m.events ?? []);
          setCursor(m.events_next_seq ?? null);
        })
        .catch(() => undefined);
    };

    loadMission();
    loadAll();
    const t = setInterval(loadMission, SUPERVISOR_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [missionId, nonce]);

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
      api
        .mission(missionId, { eventsLimit: EVENTS_PAGE })
        .then((m) => {
          if (!live) return;
          setMission(m);
          setEvents(m.events ?? []);
          setCursor(m.events_next_seq ?? null);
        })
        .catch(() => undefined);
    };
    const t = setInterval(tick, TURN_POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
    // Keyed on the turn ID, so a NEW turn restarts the budget and a settled one tears the
    // interval down. Keyed on the mission's identity would restart it on every poll response.
  }, [missionId, openTurnId]);

  const loadOlder = useCallback(() => {
    if (cursor == null || loadingMore) return;
    setLoadingMore(true);
    // A CURSOR, never an offset: events arrive while the operator reads, and an offset window
    // shifts under them — duplicating rows or skipping them.
    api
      .mission(missionId, { eventsLimit: EVENTS_PAGE, before: cursor })
      .then((m) => {
        setEvents((prev) => [...prev, ...(m.events ?? [])]);
        setCursor(m.events_next_seq ?? null);
      })
      .catch(() => undefined)
      // Leaving what is already shown: a failed page is not a reason to drop the timeline.
      .finally(() => setLoadingMore(false));
  }, [missionId, cursor, loadingMore]);

  return {
    mission,
    events,
    objectives,
    context,
    cursor,
    loadingMore,
    loadOlder,
    reload,
  };
}
