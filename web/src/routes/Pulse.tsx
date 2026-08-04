import {
  AlertTriangle,
  ArrowRight,
  MessageSquare,
  RefreshCw,
  Send,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { HudFrame } from "../components/hud/HudFrame";
import { pendingLabel } from "../lib/pendingLabel";
import { ActionRow } from "../components/pulse/ActionRow";
import { Orchestrator } from "../components/pulse/Orchestrator";
import { api, ApiError } from "../lib/api";
import { engineBadge, engineName, relTime, shortCwd } from "../lib/format";
import { OPERATOR_PENDING } from "../lib/orchestratorAction";
import type {
  OrchestratorAction,
  PulseAskMatch,
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

/** Band names, previously the section headings. Now the LED's accessible name. */
const STATE_LABELS: Record<string, string> = Object.fromEntries(
  GROUPS.map((g) => [g.state, g.label]),
);

/** Canonical identity of a card's project — the id, which is unique, with the name only as a
 *  fallback for a card whose project ref predates ids. Never the name alone: `/work/a/app` and
 *  `/work/b/app` are two projects that share one name. */
function projectKey(c: PulseCard): string {
  return c.project?.id || c.project?.name || "";
}

/** Two projects with the same name are told apart by their parent directory. If they share that
 *  too the label stays ambiguous — the chips still filter correctly, since the key is the id. */
function disambiguate(label: string, cwd: string): string {
  const parts = cwd.split("/").filter(Boolean);
  const parent = parts.length > 1 ? parts[parts.length - 2] : "";
  return parent ? `${label} · ${parent}` : label;
}

/** Jump target for a card: the session view at /s/:engine/:uuid. The card `id` is the
 *  engine-qualified key ("engine:uuid"); strip the engine prefix for the route param. */
function sessionPath(card: PulseCard): string {
  const uuid = card.id.slice(card.id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(card.engine)}/${encodeURIComponent(uuid)}`;
}

/** One curated session card. ALL model-derived text (`synthesis`, `ai_summary`, the page
 *  banner, an Ask `why`) is rendered as plain text via React's default escaping — never
 *  markup — so a session title/summary can't inject into the page (#441). `why` (#522) is
 *  the Ask panel's one-line match reason, an optional extra row on the same card. */
function Card({
  card,
  why,
  pending,
  onResolved,
  onNote,
}: {
  card: PulseCard;
  why?: string;
  pending?: PulseAskMatch["pending"];
  onResolved?: (a: OrchestratorAction) => void;
  /** Where an explanation goes when the row itself is about to disappear — a 409 carrying a
   *  settled record removes the action from the card, so the reason has to outlive it. */
  onNote?: (msg: string) => void;
}) {
  const summary = card.synthesis || card.ai_summary || "";
  const intervention = card.intervention_required;
  return (
    <li className={styles.card}>
      <HudFrame />
      <div className={styles.cardHead}>
        {/* With the four section headings gone (#754) the LED is the only thing left carrying
            the band, so it has to carry it for a screen reader too — colour alone was fine
            while `Needs you` was a heading above the card and is not fine now. */}
        <span
          className={`${styles.led} ${styles[`led_${card.state}`]}`}
          role={card.live ? "status" : "img"}
          title={STATE_LABELS[card.state] ?? card.state}
          aria-label={
            card.live
              ? "agent working"
              : (STATE_LABELS[card.state] ?? card.state)
          }
        />
        <span className={styles.cardTitle}>{card.title}</span>
        {intervention && (
          <span
            className={styles.alert}
            role="img"
            aria-label={`intervention required: ${card.intervention_reason || "see session"}`}
            title={card.intervention_reason || "Intervention required"}
          >
            ⚠
          </span>
        )}
        <span className={styles.eng} aria-hidden="true">
          {engineBadge(card.engine)}
        </span>
      </div>
      {summary && <p className={styles.summary}>{summary}</p>}
      {why && <p className={styles.why}>{`// ${why}`}</p>}
      {/* The decision controls live ON the card (#754). The queue used to be a second list
          beside these cards and was a strict subset of them — every action's session already
          appeared here, so the operator read the same session twice in two visual languages
          with two different affordances. */}
      {card.pending_action && (
        <ActionRow
          action={card.pending_action}
          onResolved={onResolved}
          onNote={onNote}
          embedded
        />
      )}
      {/* Finding the session is only half the answer — "and there is something waiting for you
          in it" is the other half, and it is the reason to go there now rather than later.
          Server-supplied (`_with_pending`), never model-asserted: a hallucinated errand sends
          the operator into a session to find nothing and costs the flag its credibility. */}
      {pending && (
        <p className={styles.pending}>
          <AlertTriangle size={12} aria-hidden="true" />
          {pendingLabel(pending)}
        </p>
      )}
      {/* What the orchestrator last DID here (#777). The Activity block used to be a second
          list of near-identical boxes above the cards; this is the same information on the
          row it belongs to. Only when nothing is pending — a live action has its own controls
          directly above, and a settled summary beside them would read as a contradiction. */}
      {!card.pending_action && card.last_action && (
        <p className={styles.lastAction}>
          <span className={styles.lastVerb}>
            {card.last_action.verb.toUpperCase()}
          </span>
          <span>{card.last_action.state}</span>
          <span className={styles.sep} aria-hidden="true">
            ·
          </span>
          <span>{relTime(card.last_action.ts)}</span>
          {(card.last_action.repeats ?? 1) > 1 && (
            <span
              className={styles.lastRepeats}
              title={`${card.last_action.repeats} actions on this session`}
            >
              ×{card.last_action.repeats}
            </span>
          )}
        </p>
      )}
      {intervention && card.intervention_reason && (
        <p className={styles.reason}>{card.intervention_reason}</p>
      )}
      <div className={styles.cardFoot}>
        <span className={styles.proj} title={card.cwd}>
          {card.project.kind === "project"
            ? card.project.name
            : shortCwd(card.cwd)}
        </span>
        <span className={styles.sep} aria-hidden="true">
          ·
        </span>
        <span className={styles.age}>{relTime(card.last_activity)}</span>
        <Link
          className={styles.jump}
          to={sessionPath(card)}
          aria-label={`Jump into ${card.title}`}
        >
          Jump in <ArrowRight size={13} aria-hidden="true" />
        </Link>
      </div>
    </li>
  );
}

/** One thread turn in the Ask panel (#522): the user's question, or the assistant's answer
 *  line plus its matched session cards. */
interface AskTurn {
  role: "user" | "assistant";
  content: string;
  matches?: PulseAskMatch[];
}

/** Ask — the natural-language session finder embedded at the top of Pulse (#522). The
 *  conversation is stateless on the server: this panel holds the thread and replays a
 *  bounded tail with every question (the server clamps again). Matches render as the
 *  existing Pulse Card (same "Jump in"), each with the model's one-line `why`. The panel
 *  pre-gates on the reused ai_review endpoint (`pulse.configured`) — unconfigured shows a
 *  disabled input + Settings hint and makes NO call; 409/502 from the backstops surface as
 *  a note/error row. */
function AskPanel({ configured }: { configured: boolean }) {
  const [thread, setThread] = useState<AskTurn[]>([]);
  const [input, setInput] = useState("");
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);

  // Keep the newest turn in view as the thread grows (the thread scrolls, not the page).
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [thread, asking]);

  const submit = useCallback(async () => {
    const query = input.trim();
    if (!query || asking || !configured) return;
    setAsking(true);
    setNote(null);
    setFailed(null);
    setInput("");
    // The replayed history is the thread BEFORE this question (bounded server-side too).
    const history = thread
      .map((t) => ({ role: t.role, content: t.content }))
      .slice(-8);
    setThread((prev) => [...prev, { role: "user", content: query }]);
    try {
      const r = await api.pulseAsk(query, history);
      setThread((prev) => [
        ...prev,
        { role: "assistant", content: r.answer, matches: r.matches },
      ]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Busy or (backstop) unconfigured — the server detail says which.
        setNote(e.message);
      } else {
        setFailed(
          e instanceof ApiError ? e.message : "Ask failed — please try again.",
        );
      }
    } finally {
      setAsking(false);
    }
  }, [asking, configured, input, thread]);

  return (
    <section className={styles.ask} aria-label="Ask about your past work">
      <HudFrame />
      <h2 className={styles.askHead}>
        <MessageSquare
          size={14}
          className={styles.askIcon}
          aria-hidden="true"
        />
        Ask
        <span className={styles.sl} aria-hidden="true">
          //
        </span>
        <span className={styles.askSub}>
          find past sessions in plain language
        </span>
      </h2>

      {thread.length > 0 && (
        <div
          className={styles.askThread}
          ref={threadRef}
          role="log"
          aria-label="Ask conversation"
        >
          {thread.map((t, i) =>
            t.role === "user" ? (
              <p key={i} className={styles.askUser}>
                {t.content}
              </p>
            ) : (
              <div key={i} className={styles.askReply}>
                {t.content && <p className={styles.askAnswer}>{t.content}</p>}
                {t.matches && t.matches.length > 0 && (
                  <ul className={styles.askCards}>
                    {t.matches.map((m) => (
                      <Card
                        key={m.id}
                        card={m}
                        why={m.why}
                        pending={m.pending}
                      />
                    ))}
                  </ul>
                )}
              </div>
            ),
          )}
          {asking && <p className={styles.askBusy}>Searching your sessions…</p>}
        </div>
      )}

      {note && <p className={styles.note}>{note}</p>}
      {failed && <p className={styles.err}>{failed}</p>}

      <form
        className={styles.askForm}
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          className={styles.askInput}
          type="text"
          value={input}
          maxLength={2000}
          disabled={!configured || asking}
          placeholder={
            configured
              ? "e.g. I worked on the websocket reconnect bug — which session was that?"
              : "Configure the AI endpoint to ask about your work"
          }
          aria-label="Ask about your past work"
          onChange={(e) => setInput(e.target.value)}
        />
        <button
          type="submit"
          className={styles.askBtn}
          disabled={!configured || asking || !input.trim()}
          aria-label="Ask"
        >
          <Send size={14} aria-hidden="true" />
          {asking ? "Asking…" : "Ask"}
        </button>
      </form>
      {!configured && (
        <p className={styles.askHint}>
          Needs the AI endpoint — configure it in{" "}
          <Link to="/settings/ai-review">Settings → AI Review</Link>.
        </p>
      )}
    </section>
  );
}

/** Pulse — the AI-curated recent-work overview (#441 Phase 5). Reads the cached overview and
 *  renders it instantly: a top "state of your work" banner (depth ≥ medium), then recent
 *  sessions grouped by state, each with a one-click Jump in. Scans run on demand here ("Scan
 *  now", at the selected depth) or on the background loop; the GET is cache-only and never
 *  scans. Default export so it can be React.lazy-loaded from the route. */
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
  // Keyed by `project.id`, never by the display name: names are not unique — two checkouts both
  // called `app` under different parents are two different projects, and keying by name merged
  // them into one chip that then showed both. When two projects genuinely share a label the
  // parent directory disambiguates the *text*; the key stays the id either way, so filtering is
  // correct even in the residual case where the parents collide too.
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
            label: c.project?.name || key,
            n: 1,
            cwd: c.cwd || "",
          });
      }
      if (c.engine) engines.set(c.engine, (engines.get(c.engine) ?? 0) + 1);
    }
    const ambiguous = new Map<string, number>();
    for (const v of projects.values())
      ambiguous.set(v.label, (ambiguous.get(v.label) ?? 0) + 1);
    const bySize = (a: Facet, b: Facet) =>
      b.n - a.n || a.label.localeCompare(b.label);
    return {
      projects: [...projects.entries()]
        .map(([key, v]) => ({
          key,
          label:
            (ambiguous.get(v.label) ?? 0) > 1
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

  const hasCards = (overview?.cards.length ?? 0) > 0;

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

      {/* Ask leads (#522, restored): it is the surface you arrive WITH a question for, and it
          answers in one line. #726 put the AUTONOMY strip above it, which pushed the chat below
          a decision queue that grows without bound — on a phone that meant scrolling past every
          pending escalation to reach the one control you came to use. The queue is what you
          arrive to READ; the chat is what you arrive to USE, so the chat goes first. */}
      <AskPanel configured={cfg?.configured ?? false} />

      {/* The state-of-your-work summary sits directly under Ask, above the queue: it is the
          orientation you read FIRST — what happened while you were away and what is waiting —
          and below a queue that grows without bound it was effectively unreachable. */}
      {overview?.banner && (
        <section className={styles.banner} aria-label="State of your work">
          <HudFrame />
          <Sparkles
            size={15}
            className={styles.bannerIcon}
            aria-hidden="true"
          />
          <p className={styles.bannerText}>{overview.banner}</p>
        </section>
      )}

      <Orchestrator
        onTierChange={refreshConfig}
        onActionsChanged={reloadOverview}
        refreshKey={orchEpoch}
      />

      {loading ? (
        <p className={styles.state}>Loading…</p>
      ) : !hasCards ? (
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>
            No work in the last {windowDays} days
          </p>
          <p className={styles.emptyHint}>
            {overview?.generated_at
              ? "Nothing recent to surface. Start a session, or widen the window in Settings."
              : "Run a scan to curate your recent sessions."}
          </p>
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
      ) : cards.length === 0 ? (
        // There ARE cards; this selection just matches none of them. Without this the list area
        // went blank with no explanation and no obvious way back.
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>No sessions match these filters</p>
          <p className={styles.emptyHint}>
            {facets.total} session{facets.total === 1 ? "" : "s"} in the last{" "}
            {windowDays} days, none in this combination.
          </p>
          <button
            type="button"
            className={`${styles.scanBtn} shine`}
            onClick={() => {
              setProjectFilter(null);
              setEngineFilter(null);
            }}
          >
            Show all sessions
          </button>
        </div>
      ) : (
        <ul className={styles.cards} aria-label="Recent sessions">
          {cards.map((c) => (
            <Card
              key={c.id}
              card={c}
              onResolved={reloadOverview}
              onNote={setNote}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
