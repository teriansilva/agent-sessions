import { ArrowRight, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useConfig } from "../app/config";
import { api, ApiError } from "../lib/api";
import { engineBadge, engineColor, relTime, shortCwd } from "../lib/format";
import type { PulseCard, PulseDepth, PulseOverview, PulseState } from "../types/api";
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

/** Jump target for a card: the session view at /s/:engine/:uuid. The card `id` is the
 *  engine-qualified key ("engine:uuid"); strip the engine prefix for the route param. */
function sessionPath(card: PulseCard): string {
  const uuid = card.id.slice(card.id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(card.engine)}/${encodeURIComponent(uuid)}`;
}

/** One curated session card. ALL model-derived text (`synthesis`, `ai_summary`, the page
 *  banner) is rendered as plain text via React's default escaping — never markup — so a
 *  session title/summary can't inject into the page (#441). */
function Card({ card }: { card: PulseCard }) {
  const summary = card.synthesis || card.ai_summary || "";
  const intervention = card.intervention_required;
  return (
    <li
      className={`${styles.card} ${styles[card.state]}`}
      style={{ ["--eng" as string]: engineColor(card.engine) }}
    >
      <div className={styles.cardHead}>
        <span
          className={`${styles.led} ${styles[`led_${card.state}`]}`}
          role={card.live ? "status" : undefined}
          aria-label={card.live ? "agent working" : undefined}
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
      {intervention && card.intervention_reason && (
        <p className={styles.reason}>{card.intervention_reason}</p>
      )}
      <div className={styles.cardFoot}>
        <span className={styles.proj} title={card.cwd}>
          {card.project.kind === "project" ? card.project.name : shortCwd(card.cwd)}
        </span>
        <span className={styles.sep} aria-hidden="true">
          ·
        </span>
        <span className={styles.age}>{relTime(card.last_activity)}</span>
        <Link className={styles.jump} to={sessionPath(card)} aria-label={`Jump into ${card.title}`}>
          Jump in <ArrowRight size={13} aria-hidden="true" />
        </Link>
      </div>
    </li>
  );
}

/** Pulse — the AI-curated recent-work overview (#441 Phase 5). Reads the cached overview and
 *  renders it instantly: a top "state of your work" banner (depth ≥ medium), then recent
 *  sessions grouped by state, each with a one-click Jump in. Scans run on demand here ("Scan
 *  now", at the selected depth) or on the background loop; the GET is cache-only and never
 *  scans. Default export so it can be React.lazy-loaded from the route. */
export default function Pulse() {
  const cfg = useConfig()?.pulse;
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

  useEffect(() => {
    let live = true;
    api
      .pulse()
      .then((o) => live && setOverview(o))
      .catch(() => live && setError("Couldn’t load the overview."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

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
      const fresh = await api.pulseScan({ depth });
      setOverview(fresh);
      if (fresh.synthesis_skipped) {
        setNote("Synthesis needs the AI endpoint — configure it in Settings → AI Review.");
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
  }, [depth, scanning]);

  const windowDays = overview?.window_days ?? cfg?.window_days ?? 3;
  const groups = useMemo(() => {
    const cards = overview?.cards ?? [];
    return GROUPS.map((g) => ({ ...g, cards: cards.filter((c) => c.state === g.state) })).filter(
      (g) => g.cards.length > 0,
    );
  }, [overview]);

  const hasCards = (overview?.cards.length ?? 0) > 0;

  return (
    <div className={styles.pulse}>
      <header className={styles.head}>
        <div className={styles.headLeft}>
          <h1 className={styles.h1}>Pulse</h1>
          <span className={styles.window} title={`Recent window: ${windowDays} days`}>
            {windowDays}d
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
            <RefreshCw size={14} className={scanning ? styles.spin : ""} aria-hidden="true" />
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </header>

      {note && <p className={styles.note}>{note}</p>}
      {error && <p className={styles.err}>{error}</p>}

      {overview?.banner && (
        <section className={styles.banner} aria-label="State of your work">
          <Sparkles size={15} className={styles.bannerIcon} aria-hidden="true" />
          <p className={styles.bannerText}>{overview.banner}</p>
        </section>
      )}

      {loading ? (
        <p className={styles.state}>Loading…</p>
      ) : !hasCards ? (
        <div className={styles.empty}>
          <p className={styles.emptyTitle}>No work in the last {windowDays} days</p>
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
            <RefreshCw size={14} className={scanning ? styles.spin : ""} aria-hidden="true" />
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
      ) : (
        <div className={styles.groups}>
          {groups.map((g) => (
            <section key={g.state} className={styles.group} aria-labelledby={`pulse-${g.state}`}>
              <h2 id={`pulse-${g.state}`} className={styles.groupHead}>
                {g.label}
                <span className={styles.count}>{g.cards.length}</span>
              </h2>
              <ul className={styles.cards}>
                {g.cards.map((c) => (
                  <Card key={c.id} card={c} />
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
