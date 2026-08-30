import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { MissionConsole } from "../components/pulse/MissionConsole";
import { Orchestrator } from "../components/pulse/Orchestrator";
import { api, ApiError } from "../lib/api";
// `shortCwd` is for the CARD BODY only — it rewrites a `/home/<user>/` prefix and nothing else,
// so it does not shorten a `/tmp/…` path at all. It is not the fix for a raw-path filter chip
// (#803); the chips group under `Default` instead. Don't reach for it in the label path.
import { engineBadge, engineName, relTime } from "../lib/format";
import { DEFAULT_PROJECT_ID, DEFAULT_PROJECT_NAME } from "../lib/overviewGraph";
import { OPERATOR_PENDING } from "../lib/orchestratorAction";
import type {
  OrchestratorAction,
  PulseCard,
  PulseDepth,
  PulseOverview,
  PulseState,
} from "../types/api";
import styles from "./Pulse.module.css";

const DEPTHS: { id: PulseDepth; label: string }[] = [
  { id: "fast", label: "FAST" },
  { id: "medium", label: "MED" },
  { id: "slow", label: "SLOW" },
];

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
  const refreshConfig = useConfigRefresh();
  const [overview, setOverview] = useState<PulseOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [depth, setDepth] = useState<PulseDepth>(cfg?.scan_depth ?? "fast");
  const [scanning, setScanning] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // Adopt the configured depth once the config lands (it can arrive after mount).
  const [syncedDepth, setSyncedDepth] = useState(cfg?.scan_depth);
  if (cfg?.scan_depth !== syncedDepth) {
    setSyncedDepth(cfg?.scan_depth);
    if (cfg?.scan_depth) setDepth(cfg.scan_depth);
  }

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

  const changeDepth = useCallback((d: PulseDepth) => {
    setDepth(d);
    // Persist so the background loop + future scans use it; a failure is non-fatal (the next
    // Scan now still uses the selected depth via the request override).
    void api.setPrefs({ pulse: { scan_depth: d } }).catch(() => {});
  }, []);

  const scanNow = useCallback(async () => {
    if (scanning) return;
    setScanning(true);
    setNote(null);
    setError(null);
    try {
      const gen = ++overviewGen.current;
      const fresh = await api.pulseScan({ depth });
      applyOverview(gen, fresh);
      if (fresh.synthesis_skipped) {
        setNote(
          "Synthesis needs the AI endpoint — configure it in Settings → AI Review.",
        );
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNote("A scan is already running — showing the last result.");
      } else {
        setError("Scan failed — please try again.");
      }
    } finally {
      setScanning(false);
    }
  }, [depth, scanning, applyOverview]);

  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [engineFilter, setEngineFilter] = useState<string | null>(null);

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

  const windowDays = overview?.window_days ?? cfg?.window_days ?? 3;
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
      <header className={styles.head}>
        <div className={styles.headLeft}>
          <h1 className={styles.h1}>Pulse</h1>
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span
            className={styles.window}
            title={`Recent window: ${windowDays} days`}
          >
            {windowDays}d
          </span>
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span className={styles.asOf}>
            {overview?.generated_at
              ? `as of ${relTime(overview.generated_at)}`
              : "not scanned yet"}
          </span>
        </div>
        <div className={styles.headRight}>
          <div className={styles.depth} role="group" aria-label="Scan depth">
            {DEPTHS.map((d) => (
              <button
                key={d.id}
                type="button"
                className={`${styles.depthBtn} ${depth === d.id ? styles.depthOn : ""}`}
                aria-pressed={depth === d.id}
                onClick={() => changeDepth(d.id)}
              >
                {d.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className={`${styles.scanBtn} shine`}
            disabled={scanning}
            onClick={() => void scanNow()}
          >
            <RefreshCw
              size={14}
              className={scanning ? styles.spin : ""}
              aria-hidden="true"
            />
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </header>

      {note && <p className={styles.note}>{note}</p>}
      {error && <p className={styles.err}>{error}</p>}

      {/* Narrow the whole list, not just the queue (#754) — the filters reach all sessions,
          including the ones the orchestrator has said nothing about, which is most of them. */}
      {facets.total > 1 &&
        (facets.projects.length > 1 || facets.engines.length > 1) && (
          <div className={styles.filters}>
            {/* Selection is a toggle state, not just a colour: without `aria-pressed` a screen
                reader hears an identical button list whatever is filtered. */}
            <div
              className={styles.filterGroup}
              role="group"
              aria-labelledby="pulse-filter-project"
            >
              <span className={styles.filterLabel} id="pulse-filter-project">
                Project
              </span>
              <button
                type="button"
                className={`${styles.chip} ${effProject === null ? styles.chipOn : ""}`}
                aria-pressed={effProject === null}
                onClick={() => setProjectFilter(null)}
              >
                All <span className={styles.chipN}>{facets.total}</span>
              </button>
              {facets.projects.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`${styles.chip} ${effProject === f.key ? styles.chipOn : ""}`}
                  aria-pressed={effProject === f.key}
                  onClick={() =>
                    setProjectFilter(effProject === f.key ? null : f.key)
                  }
                >
                  {f.label} <span className={styles.chipN}>{f.n}</span>
                </button>
              ))}
            </div>
            {facets.engines.length > 1 && (
              <>
                <span className={styles.filterSep} aria-hidden="true" />
                <div
                  className={styles.filterGroup}
                  role="group"
                  aria-labelledby="pulse-filter-agent"
                >
                  <span className={styles.filterLabel} id="pulse-filter-agent">
                    Agent
                  </span>
                  {facets.engines.map((f) => (
                    <button
                      key={f.key}
                      type="button"
                      className={`${styles.chip} ${effEngine === f.key ? styles.chipOn : ""}`}
                      aria-pressed={effEngine === f.key}
                      // `cx` on its own is not a name. The label carries the engine's real
                      // name and its count, so the button is usable without the tooltip.
                      aria-label={`${engineName(f.key)} ${f.n}`}
                      onClick={() =>
                        setEngineFilter(effEngine === f.key ? null : f.key)
                      }
                      title={engineName(f.key)}
                    >
                      {engineBadge(f.key)}{" "}
                      <span className={styles.chipN}>{f.n}</span>
                    </button>
                  ))}
                </div>
              </>
            )}
            {(effProject || effEngine) && (
              <button
                type="button"
                className={styles.clearFilters}
                onClick={() => {
                  setProjectFilter(null);
                  setEngineFilter(null);
                }}
              >
                Clear filters
              </button>
            )}
          </div>
        )}

      {/* The AUTONOMY strip, below the console (#878).
          The rule this ordering came from is unchanged — what you arrive to USE goes above what
          you arrive to READ — but the surface it applied to has moved: Ask is no longer a
          standalone panel here, it is the console's own composer, which the console renders
          above its rail and thread. So the chat still leads the page; this strip is the
          configuration you occasionally come down to change, not the thing you came for. */}
      <Orchestrator
        onTierChange={refreshConfig}
        onActionsChanged={reloadOverview}
        refreshKey={orchEpoch}
      />

      {/* MISSION CONTROL (#878). This replaces the card grid, the state-of-your-work banner and
          the standalone Ask box — a card is now a mission row in the rail, the banner is the
          mission's own recap stream, and Ask is the composer. Nothing is orphaned by that: a
          live session with no mission lists under UNTRACKED with ADOPT. */}
      {/* The filter chips above still narrow this: `cards` is the filtered, sorted set, and it
          feeds the rail's UNTRACKED group. The chips did not belong to the grid — they belong to
          the session list, and the session list moved. */}
      <MissionConsole
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
