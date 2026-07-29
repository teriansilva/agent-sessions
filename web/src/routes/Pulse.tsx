import { ArrowRight, MessageSquare, RefreshCw, Send, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { HudFrame } from "../components/hud/HudFrame";
import { Orchestrator } from "../components/pulse/Orchestrator";
import { api, ApiError } from "../lib/api";
import { engineBadge, relTime, shortCwd } from "../lib/format";
import type {
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
function Card({ card, why }: { card: PulseCard; why?: string }) {
  const summary = card.synthesis || card.ai_summary || "";
  const intervention = card.intervention_required;
  return (
    <li className={styles.card}>
      <HudFrame />
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
      {why && <p className={styles.why}>{`// ${why}`}</p>}
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
    const history = thread.map((t) => ({ role: t.role, content: t.content })).slice(-8);
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
        setFailed(e instanceof ApiError ? e.message : "Ask failed — please try again.");
      }
    } finally {
      setAsking(false);
    }
  }, [asking, configured, input, thread]);

  return (
    <section className={styles.ask} aria-label="Ask about your past work">
      <HudFrame />
      <h2 className={styles.askHead}>
        <MessageSquare size={14} className={styles.askIcon} aria-hidden="true" />
        Ask
        <span className={styles.sl} aria-hidden="true">
          //
        </span>
        <span className={styles.askSub}>find past sessions in plain language</span>
      </h2>

      {thread.length > 0 && (
        <div className={styles.askThread} ref={threadRef} role="log" aria-label="Ask conversation">
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
                      <Card key={m.id} card={m} why={m.why} />
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
          <span className={styles.sl} aria-hidden="true">
            //
          </span>
          <span className={styles.window} title={`Recent window: ${windowDays} days`}>
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
            <RefreshCw size={14} className={scanning ? styles.spin : ""} aria-hidden="true" />
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </header>

      {note && <p className={styles.note}>{note}</p>}
      {error && <p className={styles.err}>{error}</p>}

      {/* Pulse gains agency (#726): the AUTONOMY strip + proposal feed sit above Ask,
          under the page's existing PULSE header. No new route, no new name. */}
      <Orchestrator onTierChange={refreshConfig} />

      <AskPanel configured={cfg?.configured ?? false} />

      {overview?.banner && (
        <section className={styles.banner} aria-label="State of your work">
          <HudFrame />
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
