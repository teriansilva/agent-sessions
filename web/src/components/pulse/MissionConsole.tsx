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

import { api } from "../../lib/api";
import { actionOutcome } from "../../lib/orchestratorAction";
import type {
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
import { MissionDrawer } from "./MissionDrawer";
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

const STOPS = ["THREAD", "OBJECTIVES", "TIMELINE"] as const;
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
  isCurrent: (missionId: string) => boolean;
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

  const objectives = (
    <ObjectivesPane
      objectives={d.objectives}
      context={d.context}
      loading={!d.context}
      supervisor={d.mission?.supervisor}
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
            {decisions.map((a) => (
              <ActionRow
                key={a.id}
                action={a}
                onResolved={onResolved}
                onNote={onNote}
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
  const [missions, setMissions] = useState<MissionListRow[]>([]);
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
  // `?? UNTRACKED_VIEW` is load-bearing, not defensive. With nothing selected the console still
  // renders a composer — that is the empty state, and Ask must work on a fresh install — and its
  // `missionId` is the sentinel. Comparing against a null selection would make that composer
  // never current, so every answer it received would be discarded as stale: the fence firing on
  // the one surface it was never meant to guard.
  const isCurrent = useCallback(
    (id: string) => (shownRef.current ?? UNTRACKED_VIEW) === id,
    [],
  );

  const [total, setTotal] = useState(0);
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

  const applyList = useCallback(
    (r: {
      missions: MissionListRow[];
      total?: number;
      store_error?: string | null;
    }) => {
      setMissions(r.missions);
      setTotal(r.total ?? r.missions.length);
      setStoreError(r.store_error ?? null);
    },
    [],
  );

  useEffect(() => {
    let live = true;
    const gen = listGen.current;
    api
      .missions({ limit: PAGE, archived })
      .then((r) => live && gen === listGen.current && applyList(r))
      // A read that fails empties the rail and SAYS SO; it never takes the console down, and it
      // is never conflated with "you have no missions".
      .catch(
        () => live && setStoreError("the mission list could not be loaded"),
      );
    return () => {
      live = false;
    };
  }, [applyList, archived]);

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
    const pages = Math.max(1, Math.ceil(missions.length / PAGE));
    const gen = listGen.current;
    try {
      const results = [];
      for (let i = 0; i < pages; i += 1) {
        results.push(
          await api.missions({ limit: PAGE, offset: i * PAGE, archived }),
        );
        // Checked between pages as well as at the end: a multi-page refresh is the request most
        // likely to still be running when the operator switches scope.
        if (gen !== listGen.current) return;
      }
      const last = results[results.length - 1];
      applyList({
        missions: results.flatMap((r) => r.missions),
        total: last.total,
        store_error: last.store_error,
      });
    } catch {
      /* A failed refresh leaves the rail as it stands; it never empties it. */
    }
  }, [applyList, missions.length, archived]);

  /** Follow the list rather than hard-capping it. The rail's contract is "every mission", and
   *  the first version stopped at 100 with no continuation — so mission 101 was unreachable
   *  with nothing on screen to say so. One explicit page at a time, because the alternative
   *  (fetch until exhausted on mount) makes an install with a long history pay for rows nobody
   *  asked to see. */
  const loadMoreMissions = useCallback(() => {
    if (loadingMore || missions.length >= total) return;
    setLoadingMore(true);
    const gen = listGen.current;
    api
      .missions({ limit: PAGE, offset: missions.length, archived })
      .then((r) => {
        // The append this fence exists for. Without it a page issued against the active scope
        // lands in the archived rail and mixes the two sets.
        if (gen !== listGen.current) return;
        setMissions((prev) => [...prev, ...r.missions]);
        setTotal(r.total ?? total);
      })
      .catch(() => undefined)
      .finally(() => {
        if (gen === listGen.current) setLoadingMore(false);
      });
  }, [loadingMore, missions.length, total, archived]);

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
  }, [shown]);

  /** Adoption needs a real, LIVE mission. In the UNTRACKED view `shown` is the sentinel, so the
   *  target is the first mission — and when there is none the control is disabled and says why.
   *
   *  `null` in the archived scope, deliberately: the server refuses every ordinary mutation on
   *  an archived mission ("unarchive it first", 409), so offering ADOPT there would advertise a
   *  control the backend will not honour. */
  const adoptTarget = archived
    ? null
    : shown && shown !== UNTRACKED_VIEW
      ? shown
      : (missions[0]?.id ?? null);

  /** Flipping the scope drops the explicit selection so the derivation re-picks WITHIN the new
   *  scope. Without this the console kept showing the active mission it was on while the rail
   *  listed archived ones — a body and a rail describing different sets, with nothing on screen
   *  saying so. */
  const setScope = useCallback((next: boolean) => {
    // FIRST, so every response already in flight is stale before anything else changes.
    listGen.current += 1;
    setLoadingMore(false);
    setArchived(next);
    setSelected(null);
    setMissions([]);
    setTotal(0);
    setStoreError(null);
  }, []);

  const adopt = useCallback(
    (sessionKey: string) => {
      if (!adoptTarget) return;
      setAdopting(sessionKey);
      setNote(null);
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
        .catch((e: unknown) =>
          // Exclusive membership: a session already held comes back 409 NAMING the holder, and
          // that detail is the useful half — "no" without "where it went" is not an answer.
          setNote(
            e instanceof Error
              ? e.message
              : "That session could not be adopted.",
          ),
        )
        .finally(() => setAdopting(null));
    },
    [adoptTarget, reload, onMembershipChanged],
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
          <span className={styles.missionTitle}>
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
                      onNote={setNote}
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
              isCurrent={isCurrent}
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
              isCurrent={isCurrent}
            />
          </div>
        )}
      </div>
    </div>
  );
}
