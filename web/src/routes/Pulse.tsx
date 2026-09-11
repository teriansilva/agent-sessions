import { useSectionState } from "../app/sectionState";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConfig } from "../app/config";
import { MissionConsole } from "../components/pulse/MissionConsole";
import { OrchestratorHealth } from "../components/pulse/OrchestratorHealth";
import { api } from "../lib/api";
import { engineName } from "../lib/format";
import { DEFAULT_PROJECT_ID, DEFAULT_PROJECT_NAME } from "../lib/overviewGraph";
import { OPERATOR_PENDING } from "../lib/orchestratorAction";
import type {
  OrchestratorAction,
  PulseCard,
  PulseOverview,
  PulseState,
} from "../types/api";
import styles from "./Pulse.module.css";

// DEPTHS lived here for the route's FAST/MED/SLOW selector, removed with the scan chrome
// (#929). Scan depth is configured in Settings → Pulse, which still owns it.

// Display order + label for each state bucket. needs-you first, then live, then recent, idle.
const GROUPS: { state: PulseState; label: string }[] = [
  { state: "needs_you", label: "Needs you" },
  { state: "in_flight", label: "In flight" },
  { state: "recently_active", label: "Recently active" },
  { state: "idle", label: "Idle" },
];

type Facet = { key: string; label: string; n: number };

/** Canonical identity of a card's project group.
 *
 *  For a PROJECT ENTITY: the id, which is unique, with the name only as a fallback for a card
 *  whose ref predates ids. Never the name alone: `/work/a/app` and `/work/b/app` are two
 *  projects that share one name.
 *
 *  For anything else — the `kind:"folder"` fallback a session gets when no project has adopted
 *  its cwd — the synthetic **Default** project (#445), exactly as `/api/sessions` facets and the
 *  overview graph already group it. A folder ref's `id` *is* the cwd, so keying on it gave every
 *  scratch directory its own chip labelled with a ~110-char absolute path (#803). Grouping here
 *  rather than at the two call sites is deliberate: the facet builder and the card filter share
 *  this function, so they cannot disagree about what a chip contains. */
function projectKey(c: PulseCard): string {
  if (c.project?.kind === "project")
    return c.project.id || c.project.name || "";
  return DEFAULT_PROJECT_ID;
}

/** Two projects with the same name are told apart by their parent directory. If they share that
 *  too the label stays ambiguous — the chips still filter correctly, since the key is the id. */
function disambiguate(label: string, cwd: string): string {
  const parts = cwd.split("/").filter(Boolean);
  const parent = parts.length > 1 ? parts[parts.length - 2] : "";
  return parent ? `${label} · ${parent}` : label;
}

/** One curated session card. ALL model-derived text (`synthesis`, `ai_summary`, the page
 *  banner, an Ask `why`) is rendered as plain text via React's default escaping — never
 *  markup — so a session title/summary can't inject into the page (#441). `why` (#522) is
 *  the Ask panel's one-line match reason, an optional extra row on the same card. */
// `Card` and `AskPanel` lived here. Both are gone with the grid (#878): a card is now a
// mission row in `MissionRail`, and Ask is `Composer` inside the mission thread. Their
// assertions did not go with them — see the PR's two-tier spec migration.

export default function Pulse() {
  const cfg = useConfig()?.pulse;
  const [overview, setOverview] = useState<PulseOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Bumped when an action is resolved from a card, so the orchestrator panel (which owns its
  // own pending/feed) re-reads rather than showing a count for something already decided.
  const [orchEpoch, setOrchEpoch] = useState(0);
  // Monotonic generation for overview writes. `reloadOverview` and `scanNow` race: an older
  // response arriving last would replace a newer one, and since the older snapshot predates a
  // rejection it would RESURRECT the settled action and its Approve/Reject buttons. The server
  // CAS still refuses the delivery, but the UI would be offering something already decided.
  const overviewGen = useRef(0);
  const applyOverview = useCallback((gen: number, o: PulseOverview) => {
    if (gen < overviewGen.current) return; // a newer write already landed
    overviewGen.current = gen;
    setOverview(o);
  }, []);
  // Re-fetch the cached overview. Deciding an action on a card removes it from the ledger's
  // live set, so the card must lose its controls without a reload (#754).
  const reloadOverview = useCallback(
    (settled?: OrchestratorAction) => {
      // Apply the settlement to the cards we already hold, BEFORE the refetch — and fence any
      // older response with the same generation counter. The GET can fail (offline, 5xx) and
      // its catch is deliberately silent, which left the settled action sitting on the card
      // while `ActionRow` cleared `busy` in its `finally` — so the controls came back enabled
      // for something the server had already decided, and stayed that way until a reload. The
      // decision is known from the response; it does not need a round trip to be shown.
      if (settled) {
        overviewGen.current += 1;
        setOverview((prev) => {
          if (!prev) return prev;
          const stillPending = OPERATOR_PENDING.has(settled.state);
          return {
            ...prev,
            cards: prev.cards.flatMap((c) => {
              if (c.pending_action?.id !== settled.id) return [c];
              // Mirror the server overlay: keep the row only while the action is still the
              // operator's to decide.
              if (stillPending) return [{ ...c, pending_action: settled }];
              // A card that exists only because the action did has nothing left to show —
              // dropping it beats leaving an empty phantom under "Needs you".
              if (c.synthesized_for_action) return [];
              // Undo the re-band too. `_attach_pending` overwrote `state` with `needs_you`
              // *because* of this action; with the action gone the band has to go back, or
              // the session sits under "Needs you" with nothing pending until some later
              // fetch succeeds — and the failing fetch is the case this whole branch exists
              // for.
              return [
                {
                  ...c,
                  pending_action: undefined,
                  state: c.state_without_action ?? c.state,
                },
              ];
            }),
          };
        });
      }
      const gen = ++overviewGen.current;
      api
        .pulse()
        .then((o) => applyOverview(gen, o))
        .catch(() => undefined);
      setOrchEpoch((n) => n + 1);
    },
    [applyOverview],
  );

  useEffect(() => {
    let live = true;
    const gen = ++overviewGen.current;
    api
      .pulse()
      .then((o) => live && applyOverview(gen, o))
      .catch(() => live && setError("Couldn’t load the overview."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [applyOverview]);

  // `changeDepth` and `scanNow` were the route's scan controls and went with the header
  // (#929). `routes/PulseSettings.tsx` retains both the depth/window configuration and the
  // manual scan path, so nothing here is orphaned — only unreachable from this page.

  const [projectFilter, setProjectFilter] = useSectionState<string | null>(
    "missions.untracked.project",
    null,
  );
  const [engineFilter, setEngineFilter] = useSectionState<string | null>(
    "missions.untracked.engine",
    null,
  );

  // Counts come from the UNFILTERED set, so a chip always states what selecting it would yield
  // and never vanishes because of the current selection — the same rule `/api/sessions` facets
  // follow. Filters are view state only and are never persisted, so a reload shows everything.
  //
  // Keyed by `projectKey`, never by the display name: names are not unique — two checkouts both
  // called `app` under different parents are two different projects, and keying by name merged
  // them into one chip that then showed both. When two projects genuinely share a label the
  // parent directory disambiguates the *text*; the key stays the id either way, so filtering is
  // correct even in the residual case where the parents collide too.
  //
  // Unadopted sessions all land on ONE `Default` chip (#803, via `projectKey`) — same grouping
  // the sidebar dropdown and the overview map have used since #445. A folder ref's `name` is the
  // full cwd by design (the server's `resolve()` leaves shortening to clients), so labelling a
  // chip with it put a ~110-char path in the filter row, one per scratch directory.
  const facets = useMemo(() => {
    const cards = overview?.cards ?? [];
    const projects = new Map<
      string,
      { label: string; n: number; cwd: string }
    >();
    const engines = new Map<string, number>();
    for (const c of cards) {
      const key = projectKey(c);
      if (key) {
        const cur = projects.get(key);
        if (cur) cur.n += 1;
        else
          projects.set(key, {
            label:
              key === DEFAULT_PROJECT_ID
                ? DEFAULT_PROJECT_NAME
                : c.project?.name || key,
            n: 1,
            cwd: c.cwd || "",
          });
      }
      if (c.engine) engines.set(c.engine, (engines.get(c.engine) ?? 0) + 1);
    }
    const ambiguous = new Map<string, number>();
    for (const v of projects.values())
      ambiguous.set(v.label, (ambiguous.get(v.label) ?? 0) + 1);
    // Count desc, tiebreak label — and `Default` takes its place in that ranking like any other
    // chip. The sidebar instead sorts entities by name and pins Default last (`routes/sessions.py`);
    // the difference is deliberate, not drift. These chips are count-ranked, so burying a 6-count
    // Default under a 2-count project would break the only reading the row offers.
    const bySize = (a: Facet, b: Facet) =>
      b.n - a.n || a.label.localeCompare(b.label);
    return {
      projects: [...projects.entries()]
        .map(([key, v]) => ({
          key,
          // Default is never disambiguated by a parent directory: it is one bucket spanning many
          // cwds, so `v.cwd` (whichever card landed first) would name only one of them. A user
          // project that happens to be called "Default" still gets its own suffix.
          label:
            key !== DEFAULT_PROJECT_ID && (ambiguous.get(v.label) ?? 0) > 1
              ? disambiguate(v.label, v.cwd)
              : v.label,
          n: v.n,
        }))
        .sort(bySize),
      engines: [...engines.entries()]
        .map(([key, n]) => ({ key, label: key, n }))
        .sort(bySize),
      total: cards.length,
    };
  }, [overview]);

  // A scan replaces the overview, and the project or agent you had selected may not be in the
  // new one. Left alone, a stale selection filters every card away — and if the new overview has
  // too few facets to draw the filter row, it does that with no visible control to undo it.
  //
  // So the *effective* filter is derived from the current facets rather than reconciled in an
  // effect: a selection nothing can match simply stops applying, with no extra render pass. The
  // raw selection is kept, so if a later scan brings that project back, so does its filter.
  const effProject =
    projectFilter && facets.projects.some((f) => f.key === projectFilter)
      ? projectFilter
      : null;
  const effEngine =
    engineFilter && facets.engines.some((f) => f.key === engineFilter)
      ? engineFilter
      : null;

  // ONE list, not four sections (#754). The page used to render `Needs you` / `In flight` /
  // `Recently active` / `Idle` as separate blocks, each with its own heading and its own grid —
  // so every band broke the flow and left a partial row, which at 1900px is most of the wasted
  // width the issue is about. The band is already legible per card (the LED colour, the ⚠
  // marker, and now an explicit label), and the filter chips carry the counts, so the section
  // headings were paying for themselves in whitespace only.
  //
  // The ORDER the sections conveyed is kept exactly: band priority first, then a card carrying
  // a live action ahead of one without inside that band, then whatever order the scan produced
  // (recency). Sorting rather than sectioning is what lets the grid fill every row.
  const cards = useMemo(() => {
    const all = overview?.cards ?? [];
    const rank = new Map(GROUPS.map((g, i) => [g.state, i]));
    const at = (c: PulseCard) => rank.get(c.state) ?? GROUPS.length;
    return all
      .filter(
        (c) =>
          (!effProject || projectKey(c) === effProject) &&
          (!effEngine || c.engine === effEngine),
      )
      .map((c, i) => ({ c, i }))
      .sort(
        (a, b) =>
          at(a.c) - at(b.c) ||
          Number(!!b.c.pending_action) - Number(!!a.c.pending_action) ||
          a.i - b.i,
      )
      .map((x) => x.c);
  }, [overview, effProject, effEngine]);

  return (
    <div className={styles.pulse}>
      {/* ONE HEADER, AND NO SCAN VOCABULARY (#929).
          The window selector, the FAST/MED/SLOW depth and "Scan now" were Pulse's
          operator-triggered scan model, left behind when the dashboard was replaced. They are
          removed from the ROUTE only: `routes/PulseSettings.tsx` still owns `window_days`,
          `scan_depth` and the manual scan path, so the capability is untouched and this is a
          chrome removal rather than a feature retirement.

          The live-session count replaces the scan window as orientation. The MISSION count is
          deliberately not here: the console owns the mission list, and reaching into it from the
          route would mean a second fetch of the same thing purely to render a number. */}
      <header className={styles.head}>
        <div className={styles.headLeft}>
          {/* The feature's own name (#895). The ROUTE stays `/pulse` — #840 named the page
              deliberately without renaming the URL, and changing it would break every bookmark
              and every link in the issue history for no benefit. */}
          <h1 className={styles.h1}>MISSION CONTROL</h1>
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span className={styles.asOf} data-testid="console-counts">
            {cards.length} live session{cards.length === 1 ? "" : "s"}
          </span>
        </div>
      </header>

      {error && <p className={styles.err}>{error}</p>}

      {/* Narrow the whole list, not just the queue (#754) — the filters reach all sessions,
          including the ones the orchestrator has said nothing about, which is most of them. */}
      {/* THE AUTONOMY STRIP IS GONE (#929), and its own comment is why.
          It read: "The AUTONOMY strip, below the console (#878) … what you arrive to USE goes
          above what you arrive to READ" — while rendering ABOVE the console, doing the thing it
          forbade. It was also a second control surface for settings Settings already owned, and
          its empty state literally told the operator to go there.

          What did NOT move is the evidence. `OrchestratorHealth` is read-only and renders only
          when the orchestrator is failing: #772's distinction between "nothing needed you" and
          "nothing was CHECKED" is invisible on a quiet page, so an outage has to be legible on
          the page the operator is actually on, not only on the one that can fix it. */}
      <OrchestratorHealth refreshKey={orchEpoch} />

      {/* MISSION CONTROL (#878). This replaces the card grid, the state-of-your-work banner and
          the standalone Ask box — a card is now a mission row in the rail, the banner is the
          mission's own recap stream, and Ask is the composer. Nothing is orphaned by that: a
          live session with no mission lists under UNTRACKED with ADOPT. */}
      {/* Sidebar filters narrow `cards` for the untracked view. `allCards` stays unfiltered so
          a mission never loses its decisions because of a session-list filter. */}
      <MissionConsole
        untrackedFilters={
          <div className={styles.sidebarFilters}>
            <span>Sessions without a mission</span>
            <label>
              Project
              <select
                aria-label="Filter untracked sessions by project"
                value={effProject ?? ""}
                onChange={(e) => setProjectFilter(e.target.value || null)}
              >
                <option value="">All projects</option>
                {facets.projects.map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.label} · {f.n}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Agent
              <select
                aria-label="Filter untracked sessions by agent"
                value={effEngine ?? ""}
                onChange={(e) => setEngineFilter(e.target.value || null)}
              >
                <option value="">All agents</option>
                {facets.engines.map((f) => (
                  <option key={f.key} value={f.key}>
                    {engineName(f.key)} · {f.n}
                  </option>
                ))}
              </select>
            </label>
            {effProject || effEngine ? (
              <button
                type="button"
                onClick={() => {
                  setProjectFilter(null);
                  setEngineFilter(null);
                }}
              >
                Clear session filters
              </button>
            ) : null}
          </div>
        }
        cards={cards}
        allCards={overview?.cards ?? []}
        configured={cfg?.configured ?? false}
        loading={loading}
        onActionResolved={reloadOverview}
        onMembershipChanged={reloadOverview}
        filtered={!!effProject || !!effEngine}
        onClearFilters={() => {
          setProjectFilter(null);
          setEngineFilter(null);
        }}
      />
    </div>
  );
}
