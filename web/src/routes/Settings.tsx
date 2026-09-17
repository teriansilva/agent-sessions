import encodeQR from "@paulmillr/qr";
import {
  Archive,
  ArrowLeft,
  ChevronRight,
  Code2,
  Coffee,
  Copy,
  Download,
  LogOut,
  Mail,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  type CSSProperties,
  useEffect,
  useMemo,
  useRef,
  type Dispatch,
  type SetStateAction,
  useState,
} from "react";
import {
  Link,
  Navigate,
  useLocation,
  useParams,
} from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { EnableLoginDetails } from "../components/EnableLoginDetails";
import { useOverviewPrefs } from "../app/overviewPrefs";
import { api, ApiError } from "../lib/api";
import {
  ANALYTICS_DOCS_URL,
  consentSaveError,
  saveAnalyticsConsent,
} from "../lib/analyticsConsent";
import { engineName, humanBytes, humanDuration, shortCwd } from "../lib/format";
import { stalenessNote, tone, usageCaption } from "../lib/agentUsage";
import {
  buildProjectTree,
  flattenTree,
  owningProjectId,
} from "../lib/projectTree";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { AiActivityPanel } from "./AiActivityPanel";
import { AiEndpointSetup } from "./AiEndpointSetup";
import { AiReviewSettings } from "./AiReviewSettings";
import { ForgeSettings } from "./ForgeSettings";
import { AutoSortSettings } from "./AutoSortSettings";
import { MissionPlaybooks } from "./MissionPlaybooks";
import { OrchestratorSettings } from "./OrchestratorSettings";
import { PromptsSettings } from "./PromptsSettings";
import { PulseSettings } from "./PulseSettings";
import { ProjectsManagerCard } from "./ProjectsManager";
import { RenameProjectModal } from "./RenameProjectModal";
import { ACCENT_PRESETS, normalizeAccent } from "../theme/accent";
import { useAccent } from "../theme/accentStore";
import {
  DEFAULT_TERM_FONT_SIZE,
  stepTermFontSize,
  TERM_FONT_SIZE_MAX,
  TERM_FONT_SIZE_MIN,
} from "../theme/termSize";
import {
  coerceTermFontFamily,
  DEFAULT_TERM_FONT_FAMILY,
  presetForStack,
  TERM_FONT_FAMILY_MAX_LEN,
  TERM_FONT_PRESETS,
} from "../theme/termFont";
import { isFontAvailable } from "../theme/fontAvailable";
import { useTermFont } from "../theme/termFontStore";
import { useTermSize } from "../theme/termSizeStore";
import { THEME_LIST } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import type {
  AgentUsageResponse,
  AgentUsageRow,
  AnalyticsState,
  EngineInfo,
  ProjectEntity,
  SystemInfo,
  TwoFactorEnrollment,
  UpdateInfo,
  UpdateSettings,
} from "../types/api";
import {
  ENDPOINT_LED_CLASS,
  ENDPOINT_LED_LABEL,
  useEndpointLed,
} from "../lib/aiEndpointStatus";
import { useIsMobile } from "../lib/useIsMobile";
import {
  DEFAULT_SETTINGS_SECTION,
  isSettingsSection,
  legacySettingsTarget,
  SETTINGS_GROUPS,
  SETTINGS_PATH,
  SETTINGS_SECTIONS,
  settingsGroup,
  settingsPath,
  settingsSection,
  type SettingsGroupId,
  type SettingsSectionId,
} from "./settingsTabs";
import styles from "./Settings.module.css";
import { whatsNewLabel } from "../whatsnew/due";
import { useOpenWhatsNew } from "../whatsnew/WhatsNewContext";

const BUY_ME_A_COFFEE = "https://buymeacoffee.com/teriansilva";
const SOURCE_URL = "https://github.com/teriansilva/agent-sessions";
// AGPL-3.0 §13: a network-served build must offer its users the Corresponding Source. The
// About panel already links SOURCE_URL; naming the license next to it makes the offer legible
// rather than implied — and tells an operator running a *modified* build what they owe their
// own users (repoint both at your fork).
const LICENSE = "AGPL-3.0-or-later";
const LICENSE_URL = `${SOURCE_URL}/blob/main/LICENSE`;
// Contact address kept out of the markup as a literal string (basic spam-scraper
// defence): assembled from the user + domain parts at runtime, so neither the served
// HTML nor a naive grep for the joined address finds it.
const CONTACT_USER = "contact";
const CONTACT_DOMAIN = "superstatus.io";
const contactAddr = () => `${CONTACT_USER}@${CONTACT_DOMAIN}`;

/** One group's sections, in registry order. A group holding a single section of the same name
 *  (About) renders without a group label — "ABOUT / About" would say the same word twice. */
function groupSections(group: SettingsGroupId) {
  const items = SETTINGS_SECTIONS.filter((s) => s.group === group);
  const label = settingsGroup(group).label;
  const showLabel = !(items.length === 1 && items[0].label === label);
  return { items, label, showLabel };
}

/** The Endpoint & model LED (#956). Status, not decoration: it reports the last check of the
 *  SAVED connection (`useEndpointLed`), and it carries its state as an accessible label so colour
 *  is never the only signal (docs/design.md §8). */
function EndpointLed() {
  const led = useEndpointLed(useConfig()?.ai_review);
  return (
    <span
      className={`hud-led ${ENDPOINT_LED_CLASS[led]} ${styles.navLed}`}
      role="img"
      aria-label={`AI endpoint: ${ENDPOINT_LED_LABEL[led]}`}
      title={ENDPOINT_LED_LABEL[led]}
    />
  );
}

/** The desktop settings sidebar (#956): grouped links, one per section, rendered from the
 *  registry. Links, not an ARIA tablist — each section is its own URL, so this is navigation
 *  between pages and the browser's own Tab order and history apply. The router state (the #155
 *  `returnTo`) rides along, so the back link survives every section switch. */
function SettingsNav({ active }: { active: SettingsSectionId }) {
  const location = useLocation();
  return (
    <nav className={styles.nav} aria-label="Settings">
      {SETTINGS_GROUPS.map((g) => {
        const { items, label, showLabel } = groupSections(g.id);
        return (
          <div key={g.id} className={styles.navGroup}>
            {showLabel && (
              <p className={styles.navGroupLabel} id={`settings-nav-${g.id}`}>
                {label}
              </p>
            )}
            <ul
              className={styles.navList}
              {...(showLabel
                ? { "aria-labelledby": `settings-nav-${g.id}` }
                : { "aria-label": label })}
            >
              {items.map((s) => (
                <li key={s.id}>
                  <Link
                    to={settingsPath(s.id)}
                    state={location.state}
                    className={styles.navLink}
                    aria-current={s.id === active ? "page" : undefined}
                  >
                    <span data-section-label="">{s.label}</span>
                    {s.id === "ai-endpoint" && <EndpointLed />}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

/** The active model and the LED on the phone index's Endpoint & model row (#956). */
function IndexEndpointMeta() {
  const model = useConfig()?.ai_review?.model;
  return (
    <>
      {model && <small className={styles.indexModel}>{model}</small>}
      <EndpointLed />
    </>
  );
}

/** The phone settings index (#956): a phone has no room for a sidebar, so bare `/settings` is
 *  the grouped list and each section opens full-width with a back link to it. */
function SettingsIndex({ returnTo }: { returnTo: string }) {
  const location = useLocation();
  return (
    <div className={styles.wrap}>
      <header className={styles.head}>
        <Link
          to={returnTo}
          className={styles.back}
          aria-label="Back to sessions"
        >
          <ArrowLeft size={18} />
        </Link>
        <h1>Settings</h1>
      </header>
      <nav className={styles.index} aria-label="Settings">
        {SETTINGS_GROUPS.map((g) => {
          const { items, label, showLabel } = groupSections(g.id);
          return (
            <div key={g.id} className={styles.indexGroup}>
              {showLabel && (
                <p
                  className={styles.indexGroupLabel}
                  id={`settings-index-${g.id}`}
                >
                  {label}
                </p>
              )}
              <ul
                className={styles.indexList}
                {...(showLabel
                  ? { "aria-labelledby": `settings-index-${g.id}` }
                  : { "aria-label": label })}
              >
                {items.map((s) => (
                  <li key={s.id}>
                    <Link
                      to={settingsPath(s.id)}
                      state={location.state}
                      className={styles.indexLink}
                    >
                      <span data-section-label="">{s.label}</span>
                      {s.id === "ai-endpoint" && <IndexEndpointMeta />}
                      <ChevronRight size={16} aria-hidden="true" />
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </nav>
    </div>
  );
}

/** Connected agents (discovery): every known engine with a presence dot, a "can start
 *  new" badge, and the resolved binary path. */
/** Connected agents + what each one has spent (#839).
 *
 *  One list, not two. The operator's question — "how much of this agent is left?" — is about
 *  the same row that already says whether the agent is installed, and splitting it into a second
 *  panel would make them scan two lists for one answer.
 *
 *  The section never probes on render: it shows the last answers, each labelled with when it was
 *  taken, and asking again is an explicit button. A settings page that spawns six CLIs when you
 *  open it is a settings page that hangs.
 */
function ConnectedAgents() {
  const [engines, setEngines] = useState<EngineInfo[] | null>(null);
  const [usage, setUsage] = useState<AgentUsageResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api
      .engines()
      .then((d) => alive && setEngines(d.engines))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    api
      .agentUsage()
      .then((d) => alive && setUsage(d))
      .catch(() => {
        /* usage is additive: the agent list still renders without it */
      });
    return () => {
      alive = false;
    };
  }, []);

  const rows = usage?.agents ?? [];
  const byEngine = new Map(rows.map((r) => [r.engine, r]));
  const budgets = usage?.budgets;
  const threshold = budgets?.threshold_pct ?? 90;

  // Saves are **serialized**, not merely sequence-numbered.
  //
  // Every save returns the WHOLE snapshot, so two in flight are last-response-wins: tick the
  // checkbox, type a limit, and whichever PATCH the network answers last decides what the panel
  // shows. A client-side counter cannot fix that, because request-START order is not server
  // SETTLEMENT order — partial PATCHes merge under the server's lock, so the request that
  // started first can settle last and carry the newest authoritative document. Discarding it as
  // "superseded" would throw away the only correct snapshot.
  //
  // One at a time removes the question: each PATCH is sent only after the previous has settled,
  // so the last response IS the newest state, by construction.
  const chain = useRef<Promise<unknown>>(Promise.resolve());

  // The numeric fields are CONTROLLED, with the operator's in-progress text held here and
  // dropped once the server has answered. Uncontrolled inputs cannot be reconciled at all: a
  // rejected save, or an authoritative newer snapshot, would leave the box showing a number the
  // server never accepted, with nothing on screen able to correct it.
  // A field's in-progress text, and who owns it.
  //
  // Three things have to be true at once and none of them can be inferred from the rendered
  // snapshot, because saves queue:
  //
  //  * the box shows what the operator last typed, not what the server last said;
  //  * a blur is compared against the latest value ASKED for, so "change away then back" is not
  //    mistaken for a no-op while the change-away is still in flight;
  //  * when a request settles, it clears only what IT submitted — a stale response must not drag
  //    the box back, and a failed one must not release a field a newer request has claimed.
  //
  // Ownership is a **revision token**, not the value. Matching values are not proof of ownership:
  // queue 80 → 70 → 80 and the first 80's failure sees the newest intent is also 80, releases the
  // third request's claim, and the operator's next choice then compares equal to the stored
  // snapshot — no compensating PATCH, and the queued 80 becomes the durable value. Tokens have no
  // ABA problem by construction.
  const [draft, setDraft] = useState<Record<string, string>>({});
  const fieldValue = (key: string, stored: number) =>
    draft[key] ?? (stored ? String(stored) : "");
  const rev = useRef(0);
  const owner = useRef<Record<string, number>>({});
  const intent = useRef<Record<string, number>>({});
  const intended = (key: string, stored: number) =>
    intent.current[key] ?? stored;

  /** Record that this request now speaks for these fields, and return its token. */
  function claim(values: Record<string, number>): number {
    const token = ++rev.current;
    for (const [k, v] of Object.entries(values)) {
      owner.current[k] = token;
      intent.current[k] = v;
    }
    return token;
  }

  /** Hand the fields back — only those this request still owns.
   *
   *  Success and failure do the same thing here, which is the point: either way this request is
   *  finished speaking for the field, so the box goes back to rendering the server's value and
   *  the next blur is compared against it. Releasing on failure is also what makes a rejected
   *  save retryable with the same number; without it the retry compares equal to the failed
   *  request's own intent and is never sent. */
  function release(token: number, fields: string[]) {
    const mine = fields.filter((k) => owner.current[k] === token);
    if (!mine.length) return;
    for (const k of mine) {
      delete owner.current[k];
      delete intent.current[k];
    }
    setDraft((d) => {
      if (!mine.some((k) => k in d)) return d;
      const out = { ...d };
      for (const k of mine) delete out[k];
      return out;
    });
  }

  /** Discard a field's in-progress TEXT, and nothing else.
   *
   *  Used for input the server would refuse, so nothing is sent for it — which is exactly why
   *  it must not touch `owner`/`intent`: those describe what has been **asked of the server**,
   *  and typing something invalid asks nothing. An earlier version cleared them too, on the
   *  stated assumption that no request could be outstanding. That assumption was wrong — saves
   *  are serialized, so one is frequently still queued — and it lost the claim: submit 80, then
   *  type an invalid 0 before it settles; the box snaps back to the stored 90, but with the 80's
   *  claim erased, accepting that 90 compares equal to the stored snapshot and queues nothing.
   *  The outstanding 80 then lands as the durable value.
   *
   *  Leaving the claim alone makes that case work: the box shows 90, `intended` still reads 80,
   *  so blurring 90 is a real change and a compensating PATCH goes out. */
  const dropDraft = (key: string) =>
    setDraft((d) => {
      if (!(key in d)) return d;
      const out = { ...d };
      delete out[key];
      return out;
    });

  function save(
    patch: Parameters<typeof api.setAgentBudgets>[0],
    /** field → the numeric value THIS request is asking for. */
    values: Record<string, number> = {},
  ): Promise<void> {
    const token = claim(values);
    const fields = Object.keys(values);
    const run = chain.current.then(
      async () => {
        setErr("");
        try {
          setUsage(await api.setAgentBudgets(patch));
        } catch (e) {
          // The server names what it refused and why; showing "failed" instead would leave the
          // operator to guess which field it disliked (#834).
          setErr(e instanceof Error ? e.message : "could not save");
        } finally {
          release(token, fields);
        }
      },
      () => undefined,
    );
    chain.current = run;
    return run;
  }

  async function refresh() {
    setErr("");
    setBusy(true);
    // Behind the same chain: a refresh returns a full snapshot too, so it must not overtake a
    // save that has not settled yet.
    const run = chain.current.then(
      async () => {
        try {
          setUsage(await api.agentUsageRefresh());
        } catch (e) {
          setErr(e instanceof Error ? e.message : "could not refresh");
        }
      },
      () => undefined,
    );
    chain.current = run;
    await run;
    setBusy(false);
  }

  return (
    <section className={styles.section} aria-labelledby="agents-h">
      <h2 id="agents-h">Connected agents</h2>
      <p className={styles.hint}>
        The AI-coding CLIs BattleLab can discover on this host, and what each
        one has spent. Percentages marked <em>plan</em> come from the agent
        itself; the rest are counted against a limit you set.
      </p>

      {budgets && (
        <div className={styles.budgetBar}>
          <label className={styles.budgetField}>
            Alert at
            <input
              type="number"
              min={1}
              max={100}
              value={fieldValue("threshold", threshold)}
              className={styles.budgetPct}
              aria-label="Alert threshold, percent"
              onChange={(e) => {
                // Read the value BEFORE the updater runs: `currentTarget` is null by the time
                // React invokes a deferred state updater.
                const v = e.currentTarget.value;
                setDraft((d) => ({ ...d, threshold: v }));
              }}
              onBlur={(e) => {
                const text = e.currentTarget.value;
                const v = Math.round(Number(text));
                if (
                  Number.isFinite(v) &&
                  v >= 1 &&
                  v <= 100 &&
                  v !== intended("threshold", threshold)
                ) {
                  void save({ threshold_pct: v }, { threshold: v });
                } else {
                  // Not a value the server would take — snap back rather than leave the box
                  // showing a number that was never persisted.
                  dropDraft("threshold");
                }
              }}
            />
            %
          </label>
          <label className={styles.budgetToggle}>
            <input
              type="checkbox"
              // Same pending-intent rule as the numeric fields, and for the same reason: saves
              // queue, so a checkbox controlled purely by the server snapshot still shows the
              // OLD value while a PATCH is in flight. Two quick clicks then both computed
              // `!oldValue` and enqueued the same write twice — the second toggle was silently
              // lost. The draft holds what the operator has actually asked for.
              checked={
                draft.notify !== undefined
                  ? draft.notify === "1"
                  : budgets.notify
              }
              onChange={(e) => {
                const next = e.currentTarget.checked;
                // The draft carries the pending intent for the checkbox exactly as it does for
                // the numeric fields, so two quick clicks send off-then-on rather than the same
                // write twice. `save` claims the field, so the settling request releases it.
                setDraft((d) => ({ ...d, notify: next ? "1" : "0" }));
                void save({ notify: next }, { notify: next ? 1 : 0 });
              }}
            />
            Notify me
          </label>
          <button
            type="button"
            className={styles.budgetRefresh}
            onClick={() => void refresh()}
            disabled={busy}
          >
            {busy ? "Asking…" : "Ask the agents"}
          </button>
        </div>
      )}
      {err && (
        <p className={styles.budgetError} role="alert">
          {err}
        </p>
      )}

      {engines === null ? (
        <p className={styles.hint}>…</p>
      ) : (
        <ul className={styles.agents} aria-label="Connected agents">
          {engines.map((e) => (
            <li key={e.id} className={styles.agent}>
              <div className={styles.agentHead}>
                <span
                  className={`${styles.dot} ${e.present ? styles.dotOn : styles.dotOff}`}
                  aria-hidden="true"
                />
                <span className={styles.agentName}>{engineName(e.id)}</span>
                <span className={styles.agentState}>
                  {e.present ? "installed" : "not found"}
                </span>
                {e.supports_new && (
                  <span className={styles.newBadge}>can start new</span>
                )}
                <span className={styles.agentBin}>{e.bin ?? "—"}</span>
              </div>
              {/* NOT gated on `e.present`: that flag means "a binary is on PATH", and codex
                  reports its quota from its own rollout store with no binary needed — gating
                  on it hid the meter on exactly the host this was screenshotted on. Any engine
                  reaching this list is already `is_present()` (binary OR store); `shell` has no
                  row here at all because it is absent from `ENGINES`. */}
              {byEngine.has(e.id) && (
                <AgentUsageMeter
                  row={byEngine.get(e.id)!}
                  threshold={threshold}
                  present={e.present}
                  onSave={save}
                  fieldValue={fieldValue}
                  setDraft={setDraft}
                  dropDraft={dropDraft}
                  intended={intended}
                  setIntent={(k, v) => {
                    intent.current[k] = v;
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** One agent's meter + whichever inputs its source actually needs.
 *
 *  A `plan` agent gets no inputs at all — it reports a real quota against a real plan, and
 *  offering a "limit" box there would invite the operator to configure a number the agent
 *  already knows better. */
function AgentUsageMeter({
  row,
  threshold,
  present,
  onSave,
  fieldValue,
  setDraft,
  dropDraft,
  intended,
  setIntent,
}: {
  row: AgentUsageRow;
  threshold: number;
  /** Whether a binary was found on PATH — NOT whether there is usage to show. */
  present: boolean;
  onSave: (
    p: {
      engines: Record<string, { limit_tokens?: number; manual_used?: number }>;
    },
    values: Record<string, number>,
  ) => void;
  fieldValue: (key: string, stored: number) => string;
  setDraft: Dispatch<SetStateAction<Record<string, string>>>;
  /** Discard this field's in-progress text — never its outstanding claim. */
  dropDraft: (key: string) => void;
  /** The value this field was most recently asked to be — not what the snapshot renders. */
  intended: (key: string, stored: number) => number;
  setIntent: (key: string, value: number) => void;
}) {
  const pct = row.used_pct;
  const t = tone(pct, threshold);
  const note = stalenessNote(row);
  const width = pct === null ? 0 : Math.max(0, Math.min(100, pct));

  return (
    <div className={styles.usage}>
      <div className={styles.usageRow}>
        <div
          className={`${styles.meter} ${styles[`meter_${t}`]}`}
          role="meter"
          aria-valuenow={pct ?? undefined}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${row.engine} usage`}
        >
          <span className={styles.meterFill} style={{ width: `${width}%` }} />
        </div>
        <span className={`${styles.usagePct} ${styles[`pct_${t}`]}`}>
          {pct === null ? "—" : `${Math.round(pct)}%`}
        </span>
        <span className={styles.usageSource}>{row.source}</span>
      </div>
      <p className={styles.usageCaption}>
        {/* "codex · not found · 21% of its weekly plan" reads as a contradiction without this.
            Both halves are true: the rollouts on this host record real usage, and the binary
            isn't installed any more. Say which one the number came from. */}
        {!present && pct !== null && row.source !== "manual" && (
          <span className={styles.usageStale}>from its stored history · </span>
        )}
        {usageCaption(row)}
        {note && <span className={styles.usageStale}> · {note}</span>}
      </p>
      {row.source !== "plan" && (
        <div className={styles.usageInputs}>
          <label className={styles.budgetField}>
            Limit
            <input
              type="number"
              min={0}
              step={1000}
              value={fieldValue(`${row.engine}:limit_tokens`, row.limit_tokens)}
              placeholder="tokens"
              className={styles.budgetTokens}
              aria-label={`${row.engine} token limit`}
              onChange={(e) => {
                const v = e.currentTarget.value;
                setDraft((d) => ({ ...d, [`${row.engine}:limit_tokens`]: v }));
              }}
              onBlur={(e) => {
                const text = e.currentTarget.value;
                const v = Math.round(Number(text) || 0);
                if (
                  v !==
                    intended(`${row.engine}:limit_tokens`, row.limit_tokens) &&
                  v >= 0
                ) {
                  setIntent(`${row.engine}:limit_tokens`, v);
                  onSave(
                    { engines: { [row.engine]: { limit_tokens: v } } },
                    { [`${row.engine}:limit_tokens`]: v },
                  );
                } else {
                  dropDraft(`${row.engine}:limit_tokens`);
                }
              }}
            />
          </label>
          {row.source !== "tokens" && (
            <label className={styles.budgetField}>
              Used
              <input
                type="number"
                min={0}
                step={1000}
                value={fieldValue(`${row.engine}:manual_used`, row.manual_used)}
                placeholder="tokens"
                className={styles.budgetTokens}
                aria-label={`${row.engine} tokens used`}
                onChange={(e) => {
                  const v = e.currentTarget.value;
                  setDraft((d) => ({ ...d, [`${row.engine}:manual_used`]: v }));
                }}
                onBlur={(e) => {
                  const text = e.currentTarget.value;
                  const v = Math.round(Number(text) || 0);
                  if (
                    v !==
                      intended(`${row.engine}:manual_used`, row.manual_used) &&
                    v >= 0
                  ) {
                    setIntent(`${row.engine}:manual_used`, v);
                    onSave(
                      { engines: { [row.engine]: { manual_used: v } } },
                      { [`${row.engine}:manual_used`]: v },
                    );
                  } else {
                    dropDraft(`${row.engine}:manual_used`);
                  }
                }}
              />
            </label>
          )}
        </div>
      )}
    </div>
  );
}

/** System: a tidy definition list of host capacity (fail-soft — any field may be absent). */
function SystemCard() {
  const [sys, setSys] = useState<SystemInfo | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .system()
      .then((d) => alive && setSys(d))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  const rows: { label: string; value: string | null }[] = sys
    ? [
        { label: "OS", value: sys.os ?? null },
        {
          label: "Platform",
          value: [sys.platform, sys.arch].filter(Boolean).join(" · ") || null,
        },
        {
          label: "CPU",
          value:
            sys.cpus != null
              ? sys.load
                ? `${sys.cpus} cores · load ${sys.load["1"].toFixed(2)}`
                : `${sys.cpus} cores`
              : null,
        },
        {
          label: "Memory",
          value:
            sys.mem_total != null
              ? sys.mem_available != null
                ? `${humanBytes(sys.mem_total - sys.mem_available)} / ${humanBytes(sys.mem_total)}`
                : humanBytes(sys.mem_total)
              : null,
        },
        {
          label: "Disk",
          value:
            sys.disk_total != null && sys.disk_free != null
              ? `${humanBytes(sys.disk_free)} free / ${humanBytes(sys.disk_total)}`
              : null,
        },
        {
          label: "Uptime",
          value:
            sys.uptime_seconds != null
              ? humanDuration(sys.uptime_seconds)
              : null,
        },
        { label: "Python", value: sys.python ?? null },
      ]
    : [];

  return (
    <section className={styles.section} aria-labelledby="system-h">
      <h2 id="system-h">Host</h2>
      {sys === null ? (
        <p className={styles.hint}>…</p>
      ) : (
        <dl className={styles.meta}>
          {rows
            .filter((r) => r.value != null)
            .map((r) => (
              <div key={r.label} className={styles.metaRow}>
                <dt>{r.label}</dt>
                <dd>{r.value}</dd>
              </div>
            ))}
        </dl>
      )}
    </section>
  );
}

/** Usage analytics (#1009): the operator's consent. The checkbox shows what the SERVER holds — after
 *  a failed save, the reconciled state, never the click — and is disabled while a save is in flight
 *  or when the server has analytics turned off wholesale. */
function AnalyticsCard() {
  const config = useConfig();
  const refresh = useConfigRefresh();
  // What the last save learned, shown until the config it asks for arrives. ANY newer config
  // replaces it: the setting can change elsewhere in this tab (a setup replay from Help saves over
  // this page while it stays mounted underneath), and a snapshot that outlived that would show the
  // wrong state for as long as the page is open. It cannot be rolled back by a stale read either:
  // the refresh below is issued in the same tick the snapshot is set, and ConfigContext applies
  // only the latest ISSUED read, so every config that lands after this point postdates the save.
  const [override, setOverride] = useState<{
    state: AnalyticsState | null;
  } | null>(null);
  const [seenConfig, setSeenConfig] = useState(config);
  if (config !== seenConfig) {
    setSeenConfig(config);
    if (override) setOverride(null);
  }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const state = override?.state ?? config?.analytics ?? null;

  const save = async (value: boolean) => {
    setBusy(true);
    setErr(null);
    const r = await saveAnalyticsConsent(value);
    setBusy(false);
    setOverride({ state: r.state });
    if (!r.ok) setErr(consentSaveError(r.state));
    refresh();
  };

  const available = state?.available ?? false;
  return (
    <section className={styles.section} aria-labelledby="analytics-h">
      <h2 id="analytics-h">Usage analytics</h2>
      {state && !available && (
        <p className={styles.warn}>
          Turned off for this server by AGENT_SESSIONS_ANALYTICS=0 — nothing is
          sent, and this setting can&apos;t be changed here.
        </p>
      )}
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={available && !!state?.enabled}
          disabled={!state || !available || busy}
          onChange={(e) => void save(e.currentTarget.checked)}
        />
        <span>Share usage analytics</span>
      </label>
      {err && (
        <p className={styles.err} role="alert">
          {err}
        </p>
      )}
      <p className={styles.hint}>
        When on, BattleLab sends a daily active-install report on days you open
        it, with up to three delivery attempts: a random install ID (not derived
        from this machine, your account or your network), the BattleLab version
        and your operating system. Nothing about your sessions, prompts, code,
        files, projects or hosts is sent.
      </p>
      <p className={styles.hint}>
        It goes to the BattleLab team&apos;s self-hosted Umami server, which uses
        your IP address to estimate an approximate location and does not store
        the address; the web server in front of it keeps standard access logs,
        which include it, for up to 52 days. Switching it off deletes the install
        ID and stops future reports — one already under way may still arrive;
        switching it back on creates a new ID.{" "}
        <a
          className={styles.inlineLink}
          href={ANALYTICS_DOCS_URL}
          target="_blank"
          rel="noopener noreferrer"
        >
          What exactly is sent
        </a>
      </p>
    </section>
  );
}

/** Updates: compare the running version to the channel's latest and apply (re-runs the
 *  installer). Only meaningful for installer-managed deploys; in a dev/source checkout
 *  apply returns 503 (surfaced). On the default `stable` channel with no release tags
 *  yet, the check reports "up to date" (no `latest`).
 *
 *  #538: the card also owns the persisted update settings — the daily automatic-update
 *  toggle and the release channel. Both load from the cheap `/api/update/settings` (no
 *  remote hit on mount) and save optimistically; the server applies them live. The
 *  last-automatic-check line is recent runtime status only (in-memory server-side —
 *  it resets when the service restarts). */
function UpdatesCard() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [settings, setSettings] = useState<UpdateSettings | null>(null);
  const [state, setState] = useState<
    "idle" | "checking" | "applying" | "applied" | "error"
  >("idle");
  const [msg, setMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped on every channel switch: an in-flight check that started under the previous
  // channel must not repopulate `info` under the new one (Hermes #539 race).
  const checkGen = useRef(0);

  // Show the running version + persisted settings immediately; the remote compare (a git
  // ls-remote) only runs when the user clicks "Check for updates".
  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setCurrent(v.version))
      .catch(() => {});
    api
      .updateSettings()
      .then((s) => alive && setSettings(s))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const save = async (patch: { auto_update?: boolean; channel?: string }) => {
    const prev = settings;
    if (prev) setSettings({ ...prev, ...patch });
    if (patch.channel) {
      // Invalidate the previous channel's compare NOW (not after the POST resolves): the
      // shown "update available" belonged to the old channel, and any check still in
      // flight for it must land in the void.
      checkGen.current++;
      setInfo(null);
    }
    setSaveErr(null);
    setSaving(true);
    try {
      setSettings(await api.setUpdateSettings(patch));
    } catch {
      setSettings(prev);
      setSaveErr("Couldn’t save update settings.");
    } finally {
      setSaving(false);
    }
  };

  const check = async () => {
    const gen = checkGen.current;
    setState("checking");
    setMsg(null);
    try {
      const result = await api.updateCheck();
      if (gen === checkGen.current) setInfo(result); // stale-channel response → dropped
      setState("idle");
    } catch {
      setState("error");
      setMsg("Couldn’t check for updates.");
    }
  };

  const apply = async () => {
    setState("applying");
    setMsg(null);
    try {
      await api.updateApply();
      setState("applied");
      setMsg("Updating… the app will restart shortly; reload in a moment.");
    } catch (e) {
      setState("error");
      setMsg(
        e instanceof ApiError && e.status === 503
          ? "Self-update isn’t available for this install."
          : e instanceof ApiError && e.status === 409
            ? "An update is already in progress."
            : "Update failed to start.",
      );
    }
  };

  const channel = settings?.channel ?? info?.channel ?? "stable";
  return (
    <section className={styles.section} aria-labelledby="updates-h">
      <h2 id="updates-h">Updates</h2>
      <dl className={styles.meta}>
        <div className={styles.metaRow}>
          <dt>Current</dt>
          <dd>{info?.current ?? current ?? "—"}</dd>
        </div>
      </dl>
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={settings?.auto_update ?? false}
          disabled={!settings}
          onChange={(e) => void save({ auto_update: e.currentTarget.checked })}
        />
        <span>Automatic updates</span>
      </label>
      <p className={styles.hint}>
        Checks daily and installs new releases with the same rollback-guarded
        installer as the button below (“Update now”, or “Reinstall latest” when
        you’re already current). No reinstall or terminal needed — the setting
        applies immediately.
      </p>
      {settings?.auto_update && (
        <p className={styles.hint}>
          {settings.last_auto
            ? `Last automatic check: ${new Date(settings.last_auto.ts * 1000).toLocaleString()} — ${settings.last_auto.result}`
            : "No automatic check yet since the last restart."}
        </p>
      )}
      <div
        className={styles.themes}
        role="radiogroup"
        aria-label="Release channel"
      >
        {[
          {
            id: "stable",
            label: "stable",
            description: "Tagged releases (recommended)",
          },
          {
            id: "main",
            label: "main",
            description: "Development branch — expect rough edges",
          },
        ].map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={channel === o.id}
            disabled={!settings}
            className={
              channel === o.id
                ? `${styles.themeCard} ${styles.active}`
                : styles.themeCard
            }
            onClick={() => void save({ channel: o.id })}
          >
            <span className={styles.themeName}>{o.label}</span>
            <span className={styles.themeDesc}>{o.description}</span>
          </button>
        ))}
      </div>
      <p className={styles.hint}>
        Switching back to stable waits for the next tagged release (it never
        downgrades on its own).
      </p>
      {saveErr && <p className={styles.err}>{saveErr}</p>}
      {/* THREE VERDICTS, NOT TWO (#931). "We could not tell" used to render as "You’re on the
          latest", which is the sentence a frozen install showed for 26 days while it sat on a
          release tag and `main` moved on without it. `undetermined` is reported separately so
          the panel can say so; `update_available` stays false either way, because uncertainty
          must never start an install on its own. */}
      {info &&
        (info.undetermined ? (
          <p className={styles.hint} data-testid="update-undetermined">
            Couldn’t determine whether an update is available
            {info.latest ? ` (${info.channel} is at ${info.latest})` : ""}. Not
            updating.
          </p>
        ) : info.update_available ? (
          <p className={styles.hint}>Update available: {info.latest}</p>
        ) : (
          <p className={styles.hint}>
            {info.latest
              ? `You’re on the latest (${info.latest}).`
              : "You’re up to date."}
          </p>
        ))}
      {msg && <p className={styles.hint}>{msg}</p>}
      <div className={styles.updateActions}>
        <button
          type="button"
          className={styles.updateBtn}
          onClick={check}
          disabled={state === "checking" || state === "applying" || saving}
        >
          <RefreshCw size={15} />{" "}
          {state === "checking" ? "Checking…" : "Check for updates"}
        </button>
        {/* ALWAYS REACHABLE, AND HONESTLY NAMED (#931).
            Gating this on `update_available` meant a wrong verdict was terminal: the only
            control that could move the install was hidden by the same predicate that had just
            failed, so an operator with a frozen install had nothing to press and no way to
            discover why. It is offered whenever the app is running.

            It is NOT called "Update now" when no update is offered, because `apply()` is not a
            no-op: the installer rebuilds the release, flips `current` and restarts the service
            (open terminals reconnect). Naming it for what it does is the honest half of making
            it reachable — #932 is what would let it become a true no-op. */}
        {info && (
          <button
            type="button"
            className={`${styles.updateApply} shine`}
            onClick={apply}
            disabled={state === "applying" || state === "applied" || saving}
            data-testid="update-apply"
          >
            <Download size={15} />{" "}
            {state === "applying"
              ? "Updating…"
              : info.update_available
                ? "Update now"
                : "Reinstall latest"}
          </button>
        )}
      </div>
      {/* Stated where the control is, not in a tooltip: a reinstall is a real interruption and
          the operator should know before pressing, not after (#931). Only when no update is
          offered — when one is, the restart is the thing they came for. */}
      {info && !info.update_available && (
        <p className={styles.hint} data-testid="update-restart-cost">
          Reinstalls the current release and restarts the service — open
          terminals reconnect.
        </p>
      )}
    </section>
  );
}

/** A read-once recovery-code panel: list + copy + download. The codes live only in
 *  component state and are never persisted by the SPA (issue #116). */
function RecoveryCodes({ codes, label }: { codes: string[]; label: string }) {
  const [copied, setCopied] = useState(false);
  const text = codes.join("\n");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the codes are visible to copy by hand */
    }
  };
  const download = () => {
    const url = URL.createObjectURL(
      new Blob([`${text}\n`], { type: "text/plain" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "battlelab-recovery-codes.txt";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className={styles.recoveryBox}>
      <p className={styles.warn}>{label}</p>
      <ul className={styles.recoveryList} aria-label="Recovery codes">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className={styles.twofaActions}>
        <button type="button" className={styles.secBtnGhost} onClick={copy}>
          <Copy size={14} /> {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className={styles.secBtnGhost} onClick={download}>
          <Download size={14} /> Download
        </button>
      </div>
    </div>
  );
}

/** A free-text proof field that resolves to a current TOTP code and/or the account
 *  password. We always send it as a password, and *also* as a code when it looks like a
 *  6-digit TOTP — so a genuine 6-digit account password can still authorize the action
 *  (the server tries the code first, then the password). */
function proofPayload(value: string): { code?: string; password?: string } {
  const v = value.trim();
  return /^\d{6}$/.test(v) ? { code: v, password: value } : { password: value };
}

/** Two-factor authentication (#116): enable (QR + manual key + confirm + recovery codes),
 *  disable, and regenerate recovery codes. Hidden when there is no login (auth_mode=none).
 *  The TOTP secret/recovery codes are shown once and never re-fetched. */
function TwoFactorCard() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [enroll, setEnroll] = useState<TwoFactorEnrollment | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [confirmCode, setConfirmCode] = useState("");
  const [showDisable, setShowDisable] = useState(false);
  const [showRegen, setShowRegen] = useState(false);
  const [proof, setProof] = useState("");
  const [regenCodes, setRegenCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => {
        if (alive) setEnabled(!!c.two_factor_enabled);
      })
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  // Client-side QR from the otpauth:// URI (bundled lib, no CDN). SVG scales to the box.
  const qrSvg = useMemo(
    () => (enroll ? encodeQR(enroll.otpauth_uri, "svg", { border: 2 }) : null),
    [enroll],
  );

  const reset = () => {
    setEnroll(null);
    setConfirmed(false);
    setConfirmCode("");
    setShowDisable(false);
    setShowRegen(false);
    setProof("");
    setRegenCodes(null);
    setError(null);
  };

  const begin = async () => {
    setBusy(true);
    setError(null);
    try {
      setEnroll(await api.enroll2fa());
      setConfirmed(false);
    } catch {
      setError("Couldn’t start enrollment.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.confirm2fa(confirmCode.trim());
      setEnabled(true);
      setConfirmed(true);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 400
          ? "That code didn’t match — check your authenticator and try again."
          : "Couldn’t confirm the code.",
      );
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.disable2fa(proofPayload(proof));
      setEnabled(false);
      reset();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 403
          ? "Enter a current authenticator code or your password."
          : "Couldn’t disable 2FA.",
      );
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.regenerate2fa(proofPayload(proof));
      setRegenCodes(r.recovery_codes);
      setShowRegen(false);
      setProof("");
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 403
          ? "Enter a current authenticator code or your password."
          : "Couldn’t regenerate recovery codes.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="twofa-h">
      <h2 id="twofa-h">Two-factor authentication</h2>
      <p className={styles.hint}>
        Require a 6-digit code from an authenticator app (Google Authenticator,
        Authy, 1Password, Aegis…) in addition to your password.
      </p>

      {enabled !== null && (
        <p className={styles.twofaStatus}>
          <ShieldCheck size={15} />
          <span
            className={`${styles.twofaBadge} ${enabled ? styles.twofaOn : styles.twofaOff}`}
          >
            {enabled ? "On" : "Off"}
          </span>
        </p>
      )}

      {error && <p className={styles.err}>{error}</p>}

      {/* Disabled, not mid-enrollment → offer Enable. */}
      {enabled === false && !enroll && (
        <button
          type="button"
          className={`${styles.secBtn} shine`}
          onClick={begin}
          disabled={busy}
        >
          <ShieldCheck size={15} />{" "}
          {busy ? "Starting…" : "Enable two-factor auth"}
        </button>
      )}

      {/* Enrollment in progress: QR + manual key + recovery codes + confirm. */}
      {enroll && !confirmed && (
        <div className={styles.enrollPanel}>
          <p className={styles.hint}>
            1. Scan this with your authenticator app:
          </p>
          {qrSvg && (
            <img
              className={styles.qr}
              alt="TOTP QR code"
              src={`data:image/svg+xml,${encodeURIComponent(qrSvg)}`}
            />
          )}
          <p className={styles.hint}>…or enter this key manually:</p>
          <code className={styles.manualKey}>{enroll.secret}</code>
          <p className={styles.hint}>
            2. Save these recovery codes somewhere safe — each works once if you
            lose your device. They’re shown only now.
          </p>
          <RecoveryCodes
            codes={enroll.recovery_codes}
            label="Recovery codes (shown once)"
          />
          <p className={styles.hint}>
            3. Enter the current 6-digit code to finish:
          </p>
          <input
            className={styles.codeInput}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="6-digit code"
            value={confirmCode}
            onChange={(e) => setConfirmCode(e.target.value)}
          />
          <div className={styles.twofaActions}>
            <button
              type="button"
              className={styles.secBtn}
              onClick={confirm}
              disabled={busy || confirmCode.trim().length < 6}
            >
              {busy ? "Confirming…" : "Confirm & enable"}
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={reset}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Just enabled: confirm the recovery codes were saved, then dismiss. */}
      {enroll && confirmed && (
        <div className={styles.enrollPanel}>
          <p className={styles.twofaStatus}>
            <ShieldCheck size={15} /> Two-factor authentication is on.
          </p>
          <RecoveryCodes
            codes={enroll.recovery_codes}
            label="Make sure you’ve saved your recovery codes — they won’t be shown again."
          />
          <button type="button" className={styles.secBtn} onClick={reset}>
            Done
          </button>
        </div>
      )}

      {/* Enabled: manage (regenerate codes / disable). */}
      {enabled === true && !enroll && (
        <div className={styles.twofaActions}>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setShowRegen((v) => !v);
              setShowDisable(false);
              setProof("");
              setError(null);
            }}
          >
            <RefreshCw size={14} /> Regenerate recovery codes
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setShowDisable((v) => !v);
              setShowRegen(false);
              setProof("");
              setError(null);
            }}
          >
            Disable
          </button>
        </div>
      )}

      {/* Fresh-proof prompt shared by disable + regenerate. */}
      {enabled === true && !enroll && (showDisable || showRegen) && (
        <div className={styles.enrollPanel}>
          <p className={styles.hint}>
            Enter a current authenticator code or your password to{" "}
            {showDisable
              ? "disable two-factor auth"
              : "regenerate your recovery codes"}
            .
          </p>
          <input
            className={styles.codeInput}
            type="password"
            autoComplete="off"
            placeholder="6-digit code or password"
            value={proof}
            onChange={(e) => setProof(e.target.value)}
          />
          <div className={styles.twofaActions}>
            <button
              type="button"
              className={styles.secBtn}
              onClick={showDisable ? disable : regenerate}
              disabled={busy || !proof}
            >
              {busy ? "Working…" : showDisable ? "Disable" : "Regenerate"}
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => {
                setShowDisable(false);
                setShowRegen(false);
                setProof("");
              }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Newly regenerated codes (shown once). */}
      {regenCodes && (
        <div className={styles.enrollPanel}>
          <RecoveryCodes
            codes={regenCodes}
            label="New recovery codes — the old ones no longer work. Shown only now."
          />
          <button
            type="button"
            className={styles.secBtn}
            onClick={() => setRegenCodes(null)}
          >
            Done
          </button>
        </div>
      )}
    </section>
  );
}

/** Account (#141): a Sign out button. Hidden when there's no login (auth_mode=none), like
 *  the 2FA card. Sign out clears the session server-side, then navigates to /login. */
/** Sign out. Only ever mounted by `SecurityPanel` for a login-on install, which is what makes a
 *  second `auth_mode` check here dead code (#956). */
function AccountCard() {
  const [busy, setBusy] = useState(false);

  return (
    <section className={styles.section} aria-labelledby="account-h">
      <h2 id="account-h">Account</h2>
      <p className={styles.hint}>You’re signed in to this BattleLab.</p>
      <button
        type="button"
        className={styles.secBtnGhost}
        disabled={busy}
        onClick={() => {
          setBusy(true);
          // logout hard-navigates on success; only re-enable if it threw.
          api.logout().catch(() => setBusy(false));
        }}
      >
        <LogOut size={16} /> {busy ? "Signing out…" : "Sign out"}
      </button>
    </section>
  );
}

/** Login-off explainer (#682): in Home Free / `auth_mode=none` there's no password or 2FA to
 *  manage, so the 2FA + Account cards hide and the Security tab would otherwise render empty.
 *  Show what login-off means plus the (verified) recipe to turn a password login on instead. */
function LoginOffCard() {
  return (
    <section className={styles.section} aria-labelledby="loginoff-h">
      <h2 id="loginoff-h">Login</h2>
      <p className={styles.blurb}>
        Login is off — you’re running <strong>Home Free</strong>. The app is
        bound to loopback and reached only through the blind relay with your
        access key, so there’s no in-app password or two-factor to manage here.
      </p>
      <EnableLoginDetails />
    </section>
  );
}

/** Security tab body, driven by the shared config so it never flashes the single-user cards
 *  before resolving to login-off (#682). `useConfig()` is `null` while loading — render nothing
 *  then, never assume `single-user`. */
function SecurityPanel() {
  const config = useConfig();
  if (!config) return null; // still loading — avoid a single-user flash
  if (config.auth_mode === "none") return <LoginOffCard />;
  return (
    <>
      <TwoFactorCard />
      <AccountCard />
    </>
  );
}

/** Content key over the effective discovery scope (#470). Changes exactly when the server-echoed
 *  `project_roots` / `folder_exclusions` in /api/config change, so effects that fetch the
 *  discovered folder set can depend on it without re-running for unrelated config updates. */
function useDiscoveryKey(): string {
  const config = useConfig();
  const roots = config?.project_roots ?? [];
  const exclusions = config?.folder_exclusions ?? [];
  return `${roots.join("\n")}\u0000${exclusions.join("\n")}`;
}

/** Folder discovery (#465): the operator picks the root dir(s) discovery is scoped to (a HARD
 *  scope — out-of-root folders are hidden from the sidebar too) plus a manual exclusion list for
 *  ephemerals that slip through. Empty roots ⇒ today's unscoped behaviour. Each list commits via
 *  `setPrefs`; roots/exclusions are added through the existing `~/`-rooted FolderPickerModal. */
function FolderDiscoveryCard() {
  const config = useConfig();
  // #470: a saved root/exclusion changes what /api/folders discovers — refetch /api/config so
  // the Session overview + Default project cards (keyed on the discovery prefs) refresh live.
  const refreshConfig = useConfigRefresh();
  // Optimistic local state seeded from config; reflect external changes (another device / reload)
  // via React's render-phase "adjust state on change" pattern, like the compose/default-project
  // controls. `project_roots` echoes the EFFECTIVE (normalized) list the server returns.
  const configRoots = config?.project_roots ?? [];
  const configExclusions = config?.folder_exclusions ?? [];
  const [roots, setRoots] = useState<string[]>(configRoots);
  const [exclusions, setExclusions] = useState<string[]>(configExclusions);
  const [syncedRoots, setSyncedRoots] = useState(configRoots);
  const [syncedExclusions, setSyncedExclusions] = useState(configExclusions);
  // Compare by content so a fresh array identity from config doesn't churn local edits.
  if (configRoots.join("\n") !== syncedRoots.join("\n")) {
    setSyncedRoots(configRoots);
    setRoots(configRoots);
  }
  if (configExclusions.join("\n") !== syncedExclusions.join("\n")) {
    setSyncedExclusions(configExclusions);
    setExclusions(configExclusions);
  }
  // Which picker is open ("root" | "exclusion" | null) + the trigger to refocus on close.
  const [picking, setPicking] = useState<{
    kind: "root" | "exclusion";
    trigger: HTMLElement | null;
  } | null>(null);

  const commitRoots = (next: string[]) => {
    const prev = roots;
    setRoots(next);
    // The server echoes the effective (existing-dir-only) list — apply it so a non-existent pick
    // silently drops, matching what discovery will actually use.
    api
      .setPrefs({ project_roots: next })
      .then((r) => {
        const eff = (r as { project_roots?: string[] }).project_roots;
        if (Array.isArray(eff)) setRoots(eff);
        refreshConfig();
      })
      .catch(() => setRoots(prev));
  };
  const commitExclusions = (next: string[]) => {
    const prev = exclusions;
    setExclusions(next);
    api
      .setPrefs({ folder_exclusions: next })
      .then(() => refreshConfig())
      .catch(() => setExclusions(prev));
  };

  const onPick = (path: string) => {
    if (picking?.kind === "root") {
      if (!roots.includes(path)) commitRoots([...roots, path]);
    } else if (picking?.kind === "exclusion") {
      if (!exclusions.includes(path)) commitExclusions([...exclusions, path]);
    }
    setPicking(null);
  };

  return (
    <section className={styles.section} aria-labelledby="discovery-h">
      <h2 id="discovery-h">Folder discovery</h2>
      <p className={styles.hint}>
        Scope folder discovery to your project root(s). When a root is set this
        is a hard scope — folders outside it are hidden from the sidebar,
        filter, and pickers too. With no roots, discovery is unscoped (every
        session&rsquo;s folder plus ~/claude subdirs). Add exclusions
        for scratch folders that slip through.
      </p>

      <h3 className={`${styles.aiFieldLabel} ${styles.discoverySub}`}>
        Root directories
      </h3>
      {roots.length === 0 ? (
        <p className={styles.hint}>No roots — discovery is unscoped.</p>
      ) : (
        <ul className={styles.excludeList} aria-label="Root directories">
          {roots.map((r) => (
            <li key={r} className={styles.discoveryRow}>
              <span className={styles.discoveryPath}>{shortCwd(r)}</span>
              <button
                type="button"
                className={styles.discoveryRemove}
                onClick={() => commitRoots(roots.filter((x) => x !== r))}
                aria-label={`Remove root ${shortCwd(r)}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className={styles.secBtnGhost}
        onClick={(e) => setPicking({ kind: "root", trigger: e.currentTarget })}
      >
        Add root…
      </button>

      <h3 className={`${styles.aiFieldLabel} ${styles.discoverySub}`}>
        Excluded folders
      </h3>
      {exclusions.length === 0 ? (
        <p className={styles.hint}>No exclusions.</p>
      ) : (
        <ul className={styles.excludeList} aria-label="Excluded folders">
          {exclusions.map((x) => (
            <li key={x} className={styles.discoveryRow}>
              <span className={styles.discoveryPath}>{shortCwd(x)}</span>
              <button
                type="button"
                className={styles.discoveryRemove}
                onClick={() =>
                  commitExclusions(exclusions.filter((e) => e !== x))
                }
                aria-label={`Remove exclusion ${shortCwd(x)}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className={styles.secBtnGhost}
        onClick={(e) =>
          setPicking({ kind: "exclusion", trigger: e.currentTarget })
        }
      >
        Add exclusion…
      </button>

      {picking && (
        <FolderPickerModal
          title={
            picking.kind === "root"
              ? "Choose a root directory"
              : "Choose a folder to exclude"
          }
          onPick={onPick}
          onCancel={() => setPicking(null)}
          returnFocusTo={picking.trigger}
        />
      )}
    </section>
  );
}

/** One project row in the Settings → Session overview card (#174). Indented by tree depth,
 *  inverse checkbox semantics, and the custom name opens a rename modal on click instead of
 *  an inline input.
 *
 *  #615: what unticking *does* depends on whether a project has adopted the folder, so the
 *  checkbox label says which. `projects_hidden` withholds a folder as a LAUNCH location for
 *  every row; for an UNADOPTED folder it additionally drops its sessions from the sidebar,
 *  the filter, and the map. An adopted folder's sessions are exempt server-side
 *  (`sessions.py` `_visible`) — a row must stay reachable in exactly one of the active /
 *  archived views, so hiding those is the project *archive*'s job, not this checkbox's.
 *  Pinned in both directions + both modes by `tests/test_projects.py`. */
function ProjectRow({
  cwd,
  depth,
  stale,
  hidden,
  adopted,
  currentName,
  onToggleHidden,
  onOpenRename,
}: {
  cwd: string;
  depth: number;
  stale: boolean;
  hidden: boolean;
  adopted: boolean;
  currentName: string;
  onToggleHidden: (cwd: string, hidden: boolean) => void;
  onOpenRename: (cwd: string, trigger: HTMLElement) => void;
}) {
  const displayName = currentName.trim();
  return (
    <li
      className={styles.excludeRow}
      style={{ paddingLeft: `${8 + depth * 18}px` }}
    >
      {/* Inverse: checked = visible, unchecked = hidden. Per #174 the user's mental model is
       *  "show this project? yes/no" — the previous "tick to hide" was confusing. */}
      <input
        type="checkbox"
        checked={!hidden}
        onChange={(e) => onToggleHidden(cwd, !e.target.checked)}
        aria-label={
          adopted
            ? `Offer ${shortCwd(cwd)} as a launch location`
            : `Show ${shortCwd(cwd)} in the sidebar, filter, and overview`
        }
      />
      <span className={styles.excludeMeta}>
        {/* Rename is offered only for UNADOPTED folders (#615 Phase 3). An adopted folder is
         *  grouped under — and labelled by — its PROJECT everywhere the app names a group (the
         *  sidebar, the filter, and the overview map, which reads `project_names` only for
         *  `kind === "folder"` groups in `overviewGraph.ts`), so a custom name typed on an adopted
         *  row would be stored and shown nowhere. Rather than keep a control that no-ops, THIS
         *  Settings row shows the folder's PATH as static text (no rename affordance) and points
         *  the user at the project. */}
        {adopted ? (
          <span
            className={styles.nameStatic}
            title="Named by its project — rename the project instead"
          >
            {shortCwd(cwd)}
          </span>
        ) : (
          <>
            {/* Click anywhere on the name to open the rename modal. Path is a subtitle only when a
             *  custom name is set — otherwise it would just repeat the name. */}
            <button
              type="button"
              className={styles.nameButton}
              onClick={(e) => onOpenRename(cwd, e.currentTarget)}
              aria-label={`Rename ${shortCwd(cwd)}`}
            >
              {displayName || shortCwd(cwd)}
            </button>
            {displayName && (
              <span className={styles.excludePath}>{shortCwd(cwd)}</span>
            )}
          </>
        )}
      </span>
      {stale && (
        <span className={styles.excludeStale}>not currently active</span>
      )}
    </li>
  );
}

/** One owning-entity group in the reworked Session overview (#465): an entity header (color dot +
 *  name + folder count) over that entity's discovered folders, each a `ProjectRow` (inverse-checkbox
 *  visibility toggle; rename only when unadopted, #615 Phase 3), rendered as a folder sub-tree. The
 *  synthetic "Unassigned" group reuses this with a dashed dot and no entity — its folders are the
 *  renamable ones. */
function OverviewGroup({
  name,
  color,
  rows,
  adopted,
  isVisible,
  projectNames,
  onToggleHidden,
  onOpenRename,
}: {
  name: string;
  color?: string;
  rows: { cwd: string; depth: number; stale: boolean }[];
  /** False for the synthetic "Unassigned" group — every other group IS a project entity. */
  adopted: boolean;
  isVisible: (cwd: string) => boolean;
  projectNames: Record<string, string>;
  onToggleHidden: (cwd: string, hidden: boolean) => void;
  onOpenRename: (cwd: string, trigger: HTMLElement) => void;
}) {
  return (
    <div className={styles.overviewGroup}>
      <div className={styles.overviewGroupHead}>
        <span
          className={
            color
              ? styles.overviewGroupDot
              : `${styles.overviewGroupDot} ${styles.overviewGroupDotEmpty}`
          }
          style={color ? { background: color } : undefined}
          aria-hidden="true"
        />
        <span className={styles.overviewGroupName}>{name}</span>
        <span className={styles.overviewGroupCount}>{rows.length}</span>
      </div>
      <ul className={styles.excludeList} aria-label={`Folders in ${name}`}>
        {rows.map((r) => (
          <ProjectRow
            key={r.cwd}
            cwd={r.cwd}
            depth={r.depth}
            stale={r.stale}
            hidden={!isVisible(r.cwd)}
            adopted={adopted}
            currentName={projectNames[r.cwd] ?? ""}
            onToggleHidden={onToggleHidden}
            onOpenRename={onOpenRename}
          />
        ))}
      </ul>
    </div>
  );
}

/** Session overview (#174, reworked #465): discovered launch folders grouped under their owning
 *  project entity (#361), with an "Unassigned" group for folders no entity owns. Each folder keeps
 *  its inverse-checkbox visibility toggle + rename. The all/included mode radios are preserved.
 *
 *  What hiding a folder does depends on adoption (#615). For EVERY folder it withholds the folder
 *  as a launch location (`/api/folders?visible=1` has no entity carve-out). For an UNADOPTED
 *  folder it additionally drops its sessions from the sidebar list, the project filter, and the
 *  map. An ADOPTED folder's sessions survive in the sidebar and the project filter (`sessions.py`
 *  `_visible` returns True for `kind == "project"` rows), and on the map only under `project`
 *  grouping, which mirrors that exemption via `keepsHiddenCwd` — under `folder`/`agent` grouping
 *  every cluster is cwd- or engine-keyed, so a hidden cwd hides its sessions there regardless of
 *  adoption (#424). A row must stay reachable in exactly one of the active/archived views, so
 *  hiding an adopted folder's sessions is the project ARCHIVE's job. Pinned in both directions
 *  and under both modes by
 *  `tests/test_projects.py`, and on the client by `overviewGraph.test.ts`. */
function OverviewCard() {
  const {
    hiddenProjects,
    includedProjects,
    projectsMode,
    isVisible,
    setProjectVisible,
    setProjectsMode,
    projectNames,
    setProjectName,
  } = useOverviewPrefs();
  // #470: the discovered set depends on the discovery scope. FolderDiscoveryCard refreshes
  // /api/config after a save, so keying the fetch on the EFFECTIVE (server-echoed) prefs
  // re-runs it live — and only when the scope actually changed.
  const discoveryKey = useDiscoveryKey();
  const [projects, setProjects] = useState<
    { cwd: string; label: string }[] | null
  >(null);
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  const [renaming, setRenaming] = useState<{
    cwd: string;
    trigger: HTMLElement | null;
  } | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .folders()
      .then((d) => alive && setProjects(d.folders))
      .catch(() => alive && setProjects([])); // discovery failed → empty, not a dead control
    return () => {
      alive = false;
    };
  }, [discoveryKey]);

  useEffect(() => {
    let alive = true;
    // Entities drive the grouping (#465). A failed fetch → no groups, everything Unassigned.
    // Mount-only: entity ownership doesn't depend on the discovery scope.
    api
      .projectEntities()
      .then((d) => alive && setEntities(d.projects))
      .catch(() => alive && setEntities([]));
    return () => {
      alive = false;
    };
  }, []);

  // Group the discovered (∪ curated-but-inactive ∪ named) folders by owning entity (#465). A
  // curated-but-inactive project (hidden in `all` mode, or included-but-not-currently-discovered
  // in `included` mode) and a rename for an inactive project all stay editable here. Within a
  // group, folders are still rendered as a nesting tree (buildProjectTree/flattenTree), so an
  // adopted parent/child pair indents the same way as before.
  const groups = useMemo(() => {
    const known = new Set((projects ?? []).map((p) => p.cwd));
    const all = new Set<string>([
      ...known,
      ...hiddenProjects,
      ...includedProjects,
      ...Object.keys(projectNames),
    ]);
    // Owner id → its cwds. "" is the Unassigned bucket.
    const byOwner = new Map<string, Set<string>>();
    for (const cwd of all) {
      const owner = owningProjectId(cwd, entities);
      const bucket = byOwner.get(owner) ?? new Set<string>();
      bucket.add(cwd);
      byOwner.set(owner, bucket);
    }
    const toRows = (cwds: Set<string>) =>
      flattenTree(buildProjectTree(cwds)).map((n) => ({
        cwd: n.cwd,
        depth: n.depth,
        stale: !known.has(n.cwd),
      }));
    // One group per entity that owns ≥1 discovered folder, entities first (by name), then
    // Unassigned last.
    const entityGroups = entities
      .filter((e) => (byOwner.get(e.id)?.size ?? 0) > 0)
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((e) => ({
        key: e.id,
        name: e.name,
        color: e.color || undefined,
        rows: toRows(byOwner.get(e.id)!),
      }));
    const unassigned = byOwner.get("");
    if (unassigned && unassigned.size > 0) {
      entityGroups.push({
        key: "__unassigned__",
        name: "Unassigned",
        color: undefined,
        rows: toRows(unassigned),
      });
    }
    return entityGroups;
  }, [projects, entities, hiddenProjects, includedProjects, projectNames]);

  const total = useMemo(
    () => groups.reduce((n, g) => n + g.rows.length, 0),
    [groups],
  );
  const curated = projectsMode === "included";
  return (
    <section className={styles.section} aria-labelledby="overview-h">
      <h2 id="overview-h">Session overview</h2>
      {/* Visibility mode (#335). "Show all" = the legacy denylist (untick to hide). "Only included"
       *  = a curated allowlist: only ticked projects show, and a new directory never auto-appears
       *  until you tick it (starting a session in a directory also adds it automatically). */}
      <div
        className={styles.modeRow}
        role="radiogroup"
        aria-label="Project visibility"
      >
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="projects-mode"
            checked={!curated}
            onChange={() => setProjectsMode("all")}
          />
          Show all (hide a few)
        </label>
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="projects-mode"
            checked={curated}
            onChange={() => setProjectsMode("included")}
          />
          Only included
        </label>
      </div>
      {/* #615: state what unticking actually does, per row kind. Unticking always withholds a
       *  folder as a launch location; only for an UNADOPTED folder does it also drop its
       *  sessions from the sidebar/filter/map. A project's sessions stay visible either way —
       *  archive the project to hide those. The old copy promised "hide it everywhere", which
       *  was never true for adopted folders. */}
      <p className={styles.hint}>
        {curated
          ? "Folders are grouped under their owning project. Only ticked folders are offered as launch locations; an unticked, unassigned folder also drops out of the sidebar, filter, and overview map. New directories stay hidden until you tick them (starting a session in one adds it automatically)."
          : "Folders are grouped under their owning project. Unticking a folder stops it being offered as a launch location; if no project has adopted it, its sessions also disappear from the sidebar, filter, and overview map."}
      </p>
      <p className={styles.hint}>
        A project&rsquo;s sessions stay in the sidebar and filter even with its
        folders unticked — archive the project to hide those. Click an{" "}
        <em>unassigned</em> folder&rsquo;s name to give it a custom display
        name; an adopted folder takes its label from its project, so rename the
        project (above) instead.
      </p>
      {projects === null ? (
        <p className={styles.hint}>Loading folders…</p>
      ) : total === 0 ? (
        <p className={styles.hint}>No folders discovered yet.</p>
      ) : (
        // Reuse ProjectRow's inverse-checkbox: `hidden` = NOT visible under the current mode; a
        // toggle routes through `setProjectVisible`, which writes the allowlist (included) or the
        // denylist (all) — never both (#335).
        groups.map((g) => (
          <OverviewGroup
            key={g.key}
            name={g.name}
            color={g.color}
            rows={g.rows}
            adopted={g.key !== "__unassigned__"}
            isVisible={isVisible}
            projectNames={projectNames}
            onToggleHidden={(cwd, hidden) => setProjectVisible(cwd, !hidden)}
            onOpenRename={(cwd, trigger) => setRenaming({ cwd, trigger })}
          />
        ))
      )}
      {renaming && (
        <RenameProjectModal
          cwd={renaming.cwd}
          initialName={projectNames[renaming.cwd] ?? ""}
          onCancel={() => setRenaming(null)}
          onSave={(name) => {
            setProjectName(renaming.cwd, name);
            setRenaming(null);
          }}
          returnFocusTo={renaming.trigger}
        />
      )}
    </section>
  );
}

/** Maintenance (#142): bulk-archive sessions older than N hours. Reversible (archived
 *  sessions can be unarchived); a two-step confirm guards the bulk action. */
function CleanupCard() {
  const [hours, setHours] = useState(168); // default: a week
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.archiveOlder(hours);
      setResult(
        `Archived ${r.archived} session${r.archived === 1 ? "" : "s"}` +
          (r.skipped ? ` (${r.skipped} skipped).` : "."),
      );
    } catch {
      setResult("Couldn’t archive — please try again.");
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const valid = Number.isFinite(hours) && hours > 0;

  return (
    <section className={styles.section} aria-labelledby="cleanup-h">
      <h2 id="cleanup-h">Archive old sessions</h2>
      <p className={styles.hint}>
        Archive sessions you haven’t touched in a while. Archived sessions are
        hidden from the list but can be unarchived — nothing is deleted.
      </p>
      <div className={styles.cleanupRow}>
        <label className={styles.cleanupLabel}>
          Older than
          <input
            className={styles.hoursInput}
            type="number"
            min={1}
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            aria-label="Age in hours"
          />
          hours
        </label>
        {confirming ? (
          <span className={styles.confirmRow}>
            <button
              type="button"
              className={styles.danger}
              disabled={busy}
              onClick={run}
            >
              <Archive size={16} /> {busy ? "Archiving…" : "Confirm archive"}
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className={styles.secBtnGhost}
            disabled={!valid}
            onClick={() => {
              setResult(null);
              setConfirming(true);
            }}
          >
            <Archive size={16} /> Archive older
          </button>
        )}
      </div>
      {result && <p className={styles.hint}>{result}</p>}
    </section>
  );
}

/** Scrollback cache (#206): the per-session terminal-history files that make scrollback
 *  survive restarts. Shows the cache size and lets the user reclaim it — either just the
 *  archived sessions' caches, or everything. A two-step confirm guards each clear. */
function ScrollbackCacheCard() {
  const [info, setInfo] = useState<{ bytes: number; files: number } | null>(
    null,
  );
  const [confirming, setConfirming] = useState<"all" | "archived" | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const refresh = () =>
    api
      .scrollbackInfo()
      .then(setInfo)
      .catch(() => setInfo(null));

  useEffect(() => {
    void refresh();
  }, []);

  const clear = async (scope: "all" | "archived") => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.clearScrollback(scope);
      setResult(
        `Cleared ${r.removed} cache file${r.removed === 1 ? "" : "s"} (${humanBytes(r.bytes_freed)} freed).`,
      );
      await refresh();
    } catch {
      setResult("Couldn’t clear the cache — please try again.");
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="scrollback-h">
      <h2 id="scrollback-h">Scrollback cache</h2>
      <p className={styles.hint}>
        Terminal history is cached on disk per session so scrollback survives
        restarts.
        {info
          ? ` Currently ${humanBytes(info.bytes)} across ${info.files} session${info.files === 1 ? "" : "s"}.`
          : ""}{" "}
        Clearing only drops cached scrollback — sessions and their on-disk
        transcripts are untouched.
      </p>
      {confirming ? (
        <span className={styles.confirmRow}>
          <button
            type="button"
            className={styles.danger}
            disabled={busy}
            onClick={() => void clear(confirming)}
          >
            <Trash2 size={16} />{" "}
            {busy
              ? "Clearing…"
              : confirming === "all"
                ? "Confirm clear all"
                : "Confirm clear archived"}
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => setConfirming(null)}
            disabled={busy}
          >
            Cancel
          </button>
        </span>
      ) : (
        <div className={styles.cleanupRow}>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setResult(null);
              setConfirming("archived");
            }}
          >
            <Archive size={16} /> Clear archived sessions’ cache
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setResult(null);
              setConfirming("all");
            }}
          >
            <Trash2 size={16} /> Clear all cache
          </button>
        </div>
      )}
      {result && <p className={styles.hint}>{result}</p>}
    </section>
  );
}

/** Settings (#109, tabbed in #357): a keyboard-accessible tab shell over the existing
 *  sections — Appearance, Projects, AI Review (#356 placeholder), Security, System,
 *  Maintenance, About. Deep-linkable as /settings/:tab. Reached via the gear in the topbar. */
export function Settings() {
  const { theme, setTheme } = useTheme();
  const { accent, setAccent } = useAccent();
  const { size: termFontSize, setSize: setTermFontSize } = useTermSize();
  const { family: termFontFamily, setFamily: setTermFontFamily } = useTermFont();
  // Availability is measured ONCE per mount, not per render: the answer cannot change without
  // a page reload, and the probe writes to a canvas. Asked about each preset's PRIMARY family
  // — never its stack, which always resolves because every stack ends in `monospace` (#866).
  const fontAvailability = useMemo(
    () =>
      new Map(
        TERM_FONT_PRESETS.map((f) => [
          f.id,
          f.primary === null ? true : isFontAvailable(f.primary),
        ]),
      ),
    [],
  );
  // Draft for the custom stack, committed on Enter/blur — the same shape as the accent hex
  // field above, and for the same reason: a stack is invalid for most of the time it is being
  // typed, and neither the live terminal nor the server should follow those keystrokes.
  const activePreset = presetForStack(termFontFamily);
  const [fontDraft, setFontDraft] = useState(
    activePreset ? "" : termFontFamily,
  );
  const [fontDraftError, setFontDraftError] = useState("");
  const [customOpen, setCustomOpen] = useState(!activePreset);
  // …and RECONCILE when the family changes from outside this component, which is not an edge
  // case: <ConfigProvider> renders children before /api/config resolves, so on a device with no
  // local choice this panel mounts on the default System stack and TermFontProvider seeds the
  // server's value a moment later. Without this, `customOpen` and `fontDraft` keep describing
  // the *initial* value: a server-seeded custom stack leaves every radio unchecked and the
  // Custom field closed, so the face that is actually live is neither shown nor editable.
  //
  // Render-phase "adjust state on change" (React's own pattern, used by syncedAccent /
  // syncedCompose above) rather than an effect: no extra commit, and no flash of the wrong
  // selection. An in-progress draft is NOT clobbered — `fontDraftDirty` means the operator is
  // mid-edit, and a late seed must not delete what they are typing.
  const [fontDraftDirty, setFontDraftDirty] = useState(false);
  const [syncedFamily, setSyncedFamily] = useState(termFontFamily);
  if (termFontFamily !== syncedFamily) {
    const preset = presetForStack(termFontFamily);
    if (fontDraftDirty) {
      // Mid-edit: touch NOTHING. Protecting only the draft text is not enough — the earlier
      // version still ran `setCustomOpen(!preset)`, so a late seed carrying a PRESET (the
      // common case: another device last chose Fira Code) unmounted the input under the
      // operator's cursor. The text survived and reappeared on reopening, which made it look
      // harmless; the interrupted editing surface and lost focus are the actual defect.
      // Caught in review, and the first delayed-seed regression could not see it because it
      // seeds a CUSTOM stack, which leaves `customOpen` true either way.
    } else {
      // Consuming the change is part of RECONCILING it, so the mark moves here and nowhere
      // else. Marking it synchronized up front (as the previous version did) threw the pending
      // family away while the dirty branch was deliberately ignoring it: empty the field, let a
      // preset seed land, then blur to cancel — `commitFontDraft` clears the dirty flag and
      // returns without touching the family, and because the change was already marked
      // consumed, no later render ever reconciled it. The terminal ran Fira Code while the
      // picker went on claiming Custom with an empty editor, permanently. Left here, the
      // difference is only WHEN: the comparison stays true while the operator types, costs
      // nothing (the dirty branch sets no state, so there is no render loop), and fires on the
      // first render after the draft is committed or cancelled.
      setSyncedFamily(termFontFamily);
      setCustomOpen(!preset);
      setFontDraft(preset ? "" : termFontFamily);
      setFontDraftError("");
    }
  }
  // Draft for the free-text hex field — committed on Enter/blur so mid-typing (e.g. a
  // transient valid #rgb prefix) doesn't churn the live accent or the server. When the
  // accent changes elsewhere (a preset, the colour well, another device) we reflect it into
  // the field via React's render-phase "adjust state on change" pattern (no effect needed).
  const [hexDraft, setHexDraft] = useState(accent);
  const [syncedAccent, setSyncedAccent] = useState(accent);
  if (accent !== syncedAccent) {
    setSyncedAccent(accent);
    setHexDraft(accent);
  }
  // Compose default (#254): persisted via /api/prefs; applies to sessions opened after the
  // next config load. Seed from the loaded config and reflect external changes (other device).
  const configCompose = useConfig()?.compose_default ?? "auto";
  const [composeMode, setComposeMode] = useState<string>(configCompose);
  const [syncedCompose, setSyncedCompose] = useState(configCompose);
  if (configCompose !== syncedCompose) {
    setSyncedCompose(configCompose);
    setComposeMode(configCompose);
  }
  const chooseCompose = (mode: string) => {
    const prev = composeMode;
    setComposeMode(mode);
    api.setPrefs({ compose_default: mode }).catch(() => setComposeMode(prev));
  };
  // Session list order (#506): persisted via /api/prefs; the server sorts the list. Optimistic
  // with rollback, like the others. A successful save refreshes the shared config (#548): the
  // sidebar list watches the config's order and re-sorts in place — no waiting for the poll.
  const configOrder = useConfig()?.session_list_order ?? "recent_activity";
  const refreshConfig = useConfigRefresh();
  const [listOrder, setListOrder] = useState<string>(configOrder);
  const [syncedOrder, setSyncedOrder] = useState(configOrder);
  if (configOrder !== syncedOrder) {
    setSyncedOrder(configOrder);
    setListOrder(configOrder);
  }
  const chooseOrder = (mode: string) => {
    const prev = listOrder;
    setListOrder(mode);
    api
      .setPrefs({ session_list_order: mode })
      .then(() => refreshConfig())
      .catch(() => setListOrder(prev));
  };
  const commitHex = () => {
    const norm = normalizeAccent(hexDraft);
    if (norm) setAccent(norm);
    else setHexDraft(accent); // reset an invalid entry back to the active accent
  };
  // Choosing a preset closes the custom field and clears its draft: the card grid and the input
  // are two views of ONE value, so leaving a stale draft behind would make the next Enter
  // silently overwrite the preset the operator just picked.
  const choosePreset = (stack: string) => {
    setTermFontFamily(stack);
    setFontDraft("");
    setFontDraftError("");
    setFontDraftDirty(false);
    setCustomOpen(false);
  };
  const commitFontDraft = () => {
    const raw = fontDraft.trim();
    if (!raw) {
      // An emptied field means "never mind", not "reset to default" — leaving the current face
      // alone is the answer that can't lose the operator's choice to a stray Backspace.
      setFontDraftError("");
      setFontDraft("");
      setFontDraftDirty(false);
      return;
    }
    // coerce is the READ boundary and returns the DEFAULT for anything unusable, so comparing
    // against it is how an invalid stack is detected — except when the operator genuinely typed
    // the default, which is legal and must not be reported as an error.
    const norm = coerceTermFontFamily(raw);
    if (norm !== raw && raw !== DEFAULT_TERM_FONT_FAMILY) {
      setFontDraftError(
        raw.length > TERM_FONT_FAMILY_MAX_LEN
          ? `Too long — ${TERM_FONT_FAMILY_MAX_LEN} characters max.`
          : "Not a usable font stack. Check for an unclosed quote, an empty name between commas, or a character other than letters, digits, spaces, commas, hyphens, dots and quotes.",
      );
      return;
    }
    setFontDraftError("");
    setFontDraft(norm);
    setFontDraftDirty(false);
    setTermFontFamily(norm);
  };
  const [version, setVersion] = useState<string | null>(null);
  // What's new (#971): reopened from About; null outside the shell (no dialog to open).
  const openWhatsNew = useOpenWhatsNew();
  const whatsNewButton = openWhatsNew ? whatsNewLabel() : null;
  // Return to wherever the gear was tapped from (#155) — the session, overview, or landing —
  // instead of always dropping to the new-session landing. Only trust an in-app path, and
  // never Settings itself (any tab URL) — no loop (#357).
  const location = useLocation();
  const returnTo = (() => {
    const r = (location.state as { returnTo?: unknown } | null)?.returnTo;
    return typeof r === "string" &&
      r.startsWith("/") &&
      !r.startsWith("//") &&
      r !== "/settings" &&
      !r.startsWith("/settings/")
      ? r
      : "/";
  })();
  // Canonical section from the URL (#956): /settings/:section. On desktop, bare /settings and
  // unknown sections replace-redirect to the first section; on a phone bare /settings IS the
  // index. State rides along every hop so the #155 back link survives.
  const { tab } = useParams<{ tab: string }>();
  const isMobile = useIsMobile();

  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setVersion(v.version))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  const legacy = legacySettingsTarget(tab, location.hash);
  if (legacy) {
    return <Navigate to={legacy} replace state={location.state} />;
  }
  if (tab === undefined && isMobile) {
    return <SettingsIndex returnTo={returnTo} />;
  }
  if (!isSettingsSection(tab)) {
    return (
      <Navigate
        to={
          isMobile && tab !== undefined
            ? SETTINGS_PATH
            : settingsPath(DEFAULT_SETTINGS_SECTION)
        }
        replace
        state={location.state}
      />
    );
  }
  const section = tab;
  const meta = settingsSection(section);
  const crumb =
    meta.group === "about"
      ? "Settings"
      : `Settings // ${settingsGroup(meta.group).label}`;

  return (
    <div className={isMobile ? styles.wrap : styles.split}>
      {isMobile ? (
        <header className={styles.head}>
          <Link
            to={SETTINGS_PATH}
            state={location.state}
            className={styles.back}
            aria-label="Back to settings"
          >
            <ArrowLeft size={18} />
          </Link>
          <h1>Settings</h1>
        </header>
      ) : (
        <aside className={styles.sidebar}>
          <header className={styles.head}>
            <Link
              to={returnTo}
              className={styles.back}
              aria-label="Back to sessions"
            >
              <ArrowLeft size={18} />
            </Link>
            <h1>Settings</h1>
          </header>
          <SettingsNav active={section} />
        </aside>
      )}

      <div className={isMobile ? styles.panel : styles.page}>
        <p className={styles.crumb}>
          {crumb} // <b>{meta.label}</b>
        </p>
        {section === "appearance" && (
          <>
            <section className={styles.section} aria-labelledby="appearance-h">
              <h2 id="appearance-h">Appearance</h2>
              <p className={styles.hint}>Choose how BattleLab looks.</p>
              <div
                className={styles.themes}
                role="radiogroup"
                aria-label="Theme"
              >
                {THEME_LIST.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    role="radio"
                    aria-checked={theme === t.id}
                    className={
                      theme === t.id
                        ? `${styles.themeCard} ${styles.active}`
                        : styles.themeCard
                    }
                    onClick={() => setTheme(t.id)}
                  >
                    <span
                      className={`${styles.swatch} ${styles[`sw_${t.id}`]}`}
                      aria-hidden="true"
                    />
                    <span className={styles.themeName}>{t.label}</span>
                    <span className={styles.themeDesc}>{t.description}</span>
                  </button>
                ))}
              </div>

              <h3 className={styles.subhead} id="accent-h">
                Accent
              </h3>
              <p className={styles.hint}>
                The brand colour — buttons, highlights, the terminal cursor.
              </p>
              <div
                className={styles.accents}
                role="radiogroup"
                aria-labelledby="accent-h"
              >
                {ACCENT_PRESETS.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    role="radio"
                    aria-checked={accent === p.hex}
                    aria-label={p.label}
                    title={p.label}
                    className={
                      accent === p.hex
                        ? `${styles.accentDot} ${styles.active}`
                        : styles.accentDot
                    }
                    style={{ "--dot": p.hex } as CSSProperties}
                    onClick={() => setAccent(p.hex)}
                  />
                ))}
                <label className={styles.accentCustom} title="Custom colour">
                  <input
                    type="color"
                    aria-label="Custom accent colour"
                    value={accent}
                    onChange={(e) => setAccent(e.target.value)}
                  />
                </label>
                <input
                  type="text"
                  inputMode="text"
                  spellCheck={false}
                  className={styles.accentHex}
                  aria-label="Accent hex value"
                  value={hexDraft}
                  onChange={(e) => setHexDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      commitHex();
                    }
                  }}
                  onBlur={commitHex}
                />
              </div>

              <h3 className={styles.subhead} id="termfont-h">
                Terminal font
              </h3>
              <p className={styles.hint}>
                The face every agent renders in — claude, opencode, codex, gemini,
                antigravity, kimi and a plain shell all share one terminal, so this is one
                choice, not one per engine. Faces this device doesn&rsquo;t have are greyed
                out rather than quietly falling back. Saved per device, like the size below.
              </p>
              <div
                className={styles.fonts}
                role="radiogroup"
                aria-labelledby="termfont-h"
              >
                {TERM_FONT_PRESETS.map((f) => {
                  const available = fontAvailability.get(f.id) ?? true;
                  const active = !customOpen && termFontFamily === f.stack;
                  return (
                    <button
                      key={f.id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      disabled={!available}
                      className={
                        active
                          ? `${styles.fontCard} ${styles.active}`
                          : styles.fontCard
                      }
                      onClick={() => choosePreset(f.stack)}
                    >
                      <span className={styles.fontName} style={{ fontFamily: f.stack }}>
                        {f.label}
                      </span>
                      {/* The specimen is deliberately the characters that separate one mono
                          face from another: zero vs capital O, one vs lowercase L vs capital
                          I. Rendered in the card's OWN face — that is the whole control. */}
                      <span
                        className={styles.fontSpecimen}
                        style={{ fontFamily: f.stack }}
                        aria-hidden="true"
                      >
                        0O1lI {"{}"} 8B5S
                      </span>
                      <span className={styles.fontNote}>
                        {available ? f.note : "Not on this device"}
                      </span>
                    </button>
                  );
                })}
                <button
                  type="button"
                  role="radio"
                  aria-checked={customOpen}
                  className={
                    customOpen
                      ? `${styles.fontCard} ${styles.custom} ${styles.active}`
                      : `${styles.fontCard} ${styles.custom}`
                  }
                  onClick={() => {
                    setCustomOpen(true);
                    setFontDraft((d) => d || termFontFamily);
                  }}
                >
                  <span className={styles.fontName}>Custom&hellip;</span>
                  <span
                    className={styles.fontSpecimen}
                    style={{ fontFamily: termFontFamily }}
                    aria-hidden="true"
                  >
                    0O1lI {"{}"} 8B5S
                  </span>
                  <span className={styles.fontNote}>Type a CSS stack</span>
                </button>
              </div>
              {customOpen && (
                <>
                  <div className={styles.fontCustomRow}>
                    <input
                      type="text"
                      inputMode="text"
                      spellCheck={false}
                      autoCapitalize="none"
                      autoCorrect="off"
                      maxLength={TERM_FONT_FAMILY_MAX_LEN}
                      className={
                        fontDraftError
                          ? `${styles.fontCustomInput} ${styles.invalid}`
                          : styles.fontCustomInput
                      }
                      aria-label="Custom font stack"
                      aria-invalid={fontDraftError ? true : undefined}
                      aria-describedby={
                        fontDraftError ? "termfont-err" : undefined
                      }
                      placeholder={DEFAULT_TERM_FONT_FAMILY}
                      value={fontDraft}
                      onChange={(e) => {
                        setFontDraft(e.target.value);
                        setFontDraftDirty(true);
                        if (fontDraftError) setFontDraftError("");
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          commitFontDraft();
                        }
                      }}
                      onBlur={commitFontDraft}
                    />
                  </div>
                  {fontDraftError && (
                    <p className={styles.fontCustomError} id="termfont-err" role="alert">
                      {fontDraftError}
                    </p>
                  )}
                </>
              )}

              <h3 className={styles.subhead} id="termsize-h">
                Terminal text size
              </h3>
              <p className={styles.hint}>
                Sets how many columns the agent sees. Smaller text means a wider
                terminal, which is what a column-laid-out TUI like opencode
                needs — on a phone the shipped 13&nbsp;px leaves it only about
                50 columns. Saved per device, so a phone and a desktop can
                differ.
              </p>
              <div className={styles.termSize}>
                <div
                  className={styles.termSizeStepper}
                  role="group"
                  aria-labelledby="termsize-h"
                >
                  <button
                    type="button"
                    className={styles.termSizeStep}
                    aria-label="Smaller terminal text"
                    disabled={termFontSize <= TERM_FONT_SIZE_MIN}
                    onClick={() =>
                      setTermFontSize(stepTermFontSize(termFontSize, -1))
                    }
                  >
                    &minus;
                  </button>
                  {/* aria-live so the value is announced on each step — the buttons keep focus,
                      so without it a screen-reader user gets no feedback that anything changed. */}
                  <output
                    className={styles.termSizeValue}
                    aria-live="polite"
                  >{`${termFontSize} px`}</output>
                  <button
                    type="button"
                    className={styles.termSizeStep}
                    aria-label="Bigger terminal text"
                    disabled={termFontSize >= TERM_FONT_SIZE_MAX}
                    onClick={() =>
                      setTermFontSize(stepTermFontSize(termFontSize, 1))
                    }
                  >
                    +
                  </button>
                </div>
                <button
                  type="button"
                  className={styles.termSizeReset}
                  disabled={termFontSize === DEFAULT_TERM_FONT_SIZE}
                  onClick={() => setTermFontSize(DEFAULT_TERM_FONT_SIZE)}
                >
                  {`Reset to ${DEFAULT_TERM_FONT_SIZE} px`}
                </button>
                {/* A sample at the chosen size AND the chosen face, NOT a column count:
                    Settings can be routed with no terminal mounted, so any number here would
                    be a guess. The live count belongs to the in-session quick zoom, where
                    term.cols is authoritative.

                    It renders in `termFontFamily` — until #866 this used the CHROME's
                    --font-mono stack, i.e. it previewed a face the terminal would never use.
                    The box rules and the block bar are the payload: a face without
                    U+2500/U+2580 coverage falls back per-glyph at a different advance width
                    and the right-hand column visibly drifts, which is the one defect an
                    operator must be able to see BEFORE living with the face all day. */}
                <p
                  className={styles.termSizeSample}
                  style={{
                    fontSize: `${termFontSize}px`,
                    fontFamily: termFontFamily,
                  }}
                >
                  {"┌─ opencode ───────────┬────────────┐\n"}
                  {"│ build agent-sessions │ 0O1lI 8B5S │\n"}
                  {"│ ✓ 214 passed         │ ▁▃▅▇█  62% │\n"}
                  {"└──────────────────────┴────────────┘"}
                </p>
              </div>

            </section>
          </>
        )}

        {section === "session-defaults" && (
          <>
            <section
              className={styles.section}
              aria-labelledby="session-defaults-h"
            >
              <h2 id="session-defaults-h">Session defaults</h2>
              <p className={styles.hint}>
                How a session opens and how the sidebar lists them.
              </p>

              <h3 className={styles.subhead} id="compose-h">
                Compose box
              </h3>
              <p className={styles.hint}>
                Default state when a session opens. Applies after the next
                reload.
              </p>
              <div
                className={styles.themes}
                role="radiogroup"
                aria-labelledby="compose-h"
              >
                {[
                  {
                    id: "auto",
                    label: "Auto",
                    description: "Open on touch, collapsed on desktop",
                  },
                  {
                    id: "open",
                    label: "Open",
                    description: "Always expanded on load",
                  },
                  {
                    id: "collapsed",
                    label: "Collapsed",
                    description: "Always collapsed to the bar",
                  },
                ].map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    role="radio"
                    aria-checked={composeMode === o.id}
                    className={
                      composeMode === o.id
                        ? `${styles.themeCard} ${styles.active}`
                        : styles.themeCard
                    }
                    onClick={() => chooseCompose(o.id)}
                  >
                    <span className={styles.themeName}>{o.label}</span>
                    <span className={styles.themeDesc}>{o.description}</span>
                  </button>
                ))}
              </div>

              <h3 className={styles.subhead} id="listorder-h">
                Session list order
              </h3>
              <p className={styles.hint}>
                How sessions are sorted in the sidebar. Favorites always pin to
                the top.
              </p>
              <div
                className={styles.themes}
                role="radiogroup"
                aria-labelledby="listorder-h"
              >
                {[
                  {
                    id: "recent_activity",
                    label: "Recent activity",
                    description: "Newest update first (default)",
                  },
                  {
                    id: "created_at",
                    label: "Creation date",
                    description:
                      "Newest-created first; order stays put as sessions update",
                  },
                ].map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    role="radio"
                    aria-checked={listOrder === o.id}
                    className={
                      listOrder === o.id
                        ? `${styles.themeCard} ${styles.active}`
                        : styles.themeCard
                    }
                    onClick={() => chooseOrder(o.id)}
                  >
                    <span className={styles.themeName}>{o.label}</span>
                    <span className={styles.themeDesc}>{o.description}</span>
                  </button>
                ))}
              </div>
            </section>
          </>
        )}

        {section === "projects" && (
          <>
            {/* Entities first (#361 Phase 3): what sessions BELONG to. The folder
                visibility/rename cards below stay about where sessions LAUNCH. */}
            <ProjectsManagerCard />
            {/* Folder discovery scope + exclusions (#465), above the (now entity-grouped) overview. */}
            <FolderDiscoveryCard />
            <OverviewCard />
          </>
        )}

        {/* The AI pages (#956). These eight panels used to stack in ONE column under a single
            "AI" tab; each now has the page its job warrants. */}
        {section === "ai-endpoint" && <AiEndpointSetup />}
        {section === "ai-session-review" && <AiReviewSettings />}
        {section === "ai-auto-sort" && <AutoSortSettings />}
        {section === "ai-mission-control" && (
          <>
            <OrchestratorSettings />
            <PulseSettings />
            {/* Where mission objectives are checked (#891) — the other outbound connection a
                mission's follow-through needs. */}
            <ForgeSettings />
          </>
        )}
        {/* The checklists MISSION CONTROL starts a mission with (#892). */}
        {section === "ai-playbooks" && <MissionPlaybooks />}
        {/* Every system prompt, in one catalog (#824). */}
        {section === "ai-prompts" && <PromptsSettings />}
        {section === "ai-activity" && <AiActivityPanel />}

        {section === "agents" && <ConnectedAgents />}
        {section === "security" && <SecurityPanel />}
        {section === "updates" && <UpdatesCard />}
        {section === "analytics" && <AnalyticsCard />}
        {section === "system" && <SystemCard />}
        {section === "maintenance" && (
          <>
            <CleanupCard />
            <ScrollbackCacheCard />
          </>
        )}

        {section === "about" && (
          <>
            <section className={styles.section} aria-labelledby="support-h">
              <h2 id="support-h">Support</h2>
              <p className={styles.blurb}>
                If BattleLab saves you time, you can support its development.
              </p>
              <a
                className={`${styles.coffee} shine`}
                href={BUY_ME_A_COFFEE}
                target="_blank"
                rel="noopener noreferrer"
              >
                <Coffee size={16} /> Buy me a coffee
              </a>
            </section>

            <section className={styles.section} aria-labelledby="about-h">
              <h2 id="about-h">About</h2>
              <p className={styles.brandLine}>
                Battle<b>Lab</b>
              </p>
              <p className={styles.hint}>Command &amp; Code</p>
              <p className={styles.blurb}>
                The mobile-first organizer for your AI-coding sessions — claude,
                opencode, codex, gemini, antigravity and kimi, all in one place.
              </p>
              <dl className={styles.meta}>
                <dt>Version</dt>
                <dd>{version ?? "…"}</dd>
                <dt>License</dt>
                <dd>
                  <a
                    className={styles.nameLink}
                    href={LICENSE_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {LICENSE}
                  </a>
                </dd>
                <dt>Created by</dt>
                <dd>
                  <a
                    className={styles.nameLink}
                    href="https://superstatus.io"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Marcus Braun
                  </a>
                </dd>
              </dl>
              <div className={styles.aboutLinks}>
                {openWhatsNew && whatsNewButton && (
                  <button
                    type="button"
                    className={`${styles.aboutLink} ${styles.aboutLinkButton}`}
                    onClick={openWhatsNew}
                  >
                    <Sparkles size={15} aria-hidden="true" /> {whatsNewButton}
                  </button>
                )}
                <a
                  className={styles.aboutLink}
                  href={SOURCE_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Code2 size={15} /> Source code
                </a>
                <a
                  className={styles.aboutLink}
                  href={`mailto:${contactAddr()}`}
                  onClick={(e) => {
                    // Assemble the mailto at click time so the literal address is never in the DOM at rest.
                    (e.currentTarget as HTMLAnchorElement).href =
                      `mailto:${contactAddr()}`;
                  }}
                >
                  <Mail size={15} /> {CONTACT_USER}&#64;{CONTACT_DOMAIN}
                </a>
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
