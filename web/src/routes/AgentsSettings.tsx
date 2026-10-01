/** Settings → AGENTS (#853 P4, #1128): the roster, one agent's own page, and the defaults.
 *
 *  Everything here is read from the manifests — `/api/engines` (the roster store) and
 *  `/api/engines/{id}` (one agent's detail). Nothing names an agent in code. The only things the
 *  operator edits are the budgets (moved here unchanged from "Agents & usage", #839) and the two
 *  agent defaults. */
import { AgentEndpointCard } from "../components/settings/AgentEndpointCard";
import {
  type CSSProperties,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import {
  eligibleFor,
  engineBadge,
  engineColor,
  engineInfo,
  engineLabel,
  isActive,
  markRosterFailed,
  resolveDefault,
  setRoster,
  useEngineRoster,
} from "../app/engineRoster";
import { unavailableDefaultNotice } from "../lib/agentDefaults";
import { stalenessNote, tone, usageCaption } from "../lib/agentUsage";
import { api, ApiError } from "../lib/api";
import type {
  AgentDefaults,
  AgentUsageResponse,
  AgentUsageRow,
  EngineDetail,
  EngineInfo,
  EngineProblem,
} from "../types/api";
import button from "../components/ui/actionButton.module.css";
import styles from "./Settings.module.css";
import a from "./AgentsSettings.module.css";
import { agentPath, settingsPath } from "./settingsTabs";

// ---- budgets (#839) -------------------------------------------------------------------------------

/** What each agent has spent, and the operator's budgets — the state machine that used to live in
 *  "Agents & usage", unchanged, so the roster and an agent's own page share ONE of it.
 *
 *  The pages never probe on render: they show the last answers, each labelled with when it was
 *  taken, and asking again is an explicit button. A settings page that spawns six CLIs when you
 *  open it is a settings page that hangs. */
function useAgentBudgets() {
  const [usage, setUsage] = useState<AgentUsageResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
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

  return {
    usage,
    byEngine,
    budgets,
    threshold,
    busy,
    err,
    draft,
    setDraft,
    fieldValue,
    dropDraft,
    intended,
    setIntent: (k: string, v: number) => {
      intent.current[k] = v;
    },
    save,
    refresh,
  };
}

type Budgets = ReturnType<typeof useAgentBudgets>;

/** The alert threshold, the notify switch and "Ask the agents" — global, so on the roster only. */
function BudgetBar({ b }: { b: Budgets }) {
  const {
    budgets,
    threshold,
    fieldValue,
    setDraft,
    intended,
    save,
    dropDraft,
    draft,
    refresh,
    busy,
  } = b;
  return (
    <>
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
      <BudgetError b={b} />
    </>
  );
}

/** The error line of the last budget save or refresh — the server names what it refused. */
function BudgetError({ b }: { b: Budgets }) {
  return b.err ? (
    <p className={styles.budgetError} role="alert">
      {b.err}
    </p>
  ) : null;
}

/** One engine's meter from the shared budget state, or nothing when there is no usage row. */
function EngineMeter({ b, e }: { b: Budgets; e: EngineInfo }) {
  const row = b.byEngine.get(e.id);
  if (!row) return null;
  return (
    <AgentUsageMeter
      row={row}
      threshold={b.threshold}
      present={e.present}
      onSave={b.save}
      fieldValue={b.fieldValue}
      setDraft={b.setDraft}
      dropDraft={b.dropDraft}
      intended={b.intended}
      setIntent={b.setIntent}
    />
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

/** Ask the server for the roster again, now. Same rules as the provider's load: a failure keeps
 *  the last good roster (and marks it failed, so the page can say so); it never empties it. */
function refreshRoster(): Promise<void> {
  return Promise.resolve()
    .then(() => api.engines())
    .then((d) => setRoster(d.engines, d.problems ?? []))
    .catch(() => markRosterFailed());
}

// ---- shared bits -----------------------------------------------------------------------------------

/** `pty` is the one runtime this build has, and to an operator it is a terminal. */
function runtimeLabel(runtime: string): string {
  return runtime === "pty" ? "terminal" : runtime;
}

/** The engine's accent as the `--eng` custom property its card, badge and rule read. */
const accent = (id: string) => ({ "--eng": engineColor(id) }) as CSSProperties;

type StateTone = "up" | "idle" | "deg" | "down";

/** A state tag: an LED and a word, never colour alone (docs/design.md §8). */
function StateTag({ tone: t, children }: { tone: StateTone; children: string }) {
  const led = { up: "up", idle: "idle", deg: "attention", down: "down" }[t];
  return (
    <span className={`${a.state} ${a[`state_${t}`]}`}>
      <span className={`hud-led ${led}`} aria-hidden="true" />
      {children}
    </span>
  );
}

function engineState(e: EngineInfo | undefined): {
  tone: StateTone;
  text: string;
} {
  if (!e) return { tone: "idle", text: "Unknown" };
  if (!isActive(e)) return { tone: "deg", text: "Retiring" };
  return e.present
    ? { tone: "up", text: "Present" }
    : { tone: "idle", text: "Absent" };
}

/** The capabilities a roster card shows: the ones that decide what the operator can START. The
 *  agent's own page lists all of them. */
const CARD_CAPS: readonly [keyof EngineInfo["capabilities"], string][] = [
  ["resume", "resume"],
  ["new", "new"],
  ["handoff_target", "handoff target"],
  ["orchestrator_input", "orchestrator input"],
];

// ---- /settings/agents — the roster -------------------------------------------------------------------

/** One card per roster engine. Read-only apart from the budgets; nothing here starts anything. */
function RosterCard({ e, b }: { e: EngineInfo; b: Budgets }) {
  const location = useLocation();
  const retiring = !isActive(e);
  const st = engineState(e);
  const row = b.byEngine.get(e.id);
  return (
    <li
      className={`${a.card} ${retiring || !e.present ? a.dim : ""}`}
      style={accent(e.id)}
      data-engine={e.id}
    >
      <div className={a.ctop}>
        <span className={a.badge} aria-hidden="true">
          {engineBadge(e.id)}
        </span>
        <div className={a.name}>
          <h3>{e.label}</h3>
          <span>{e.id}</span>
        </div>
        <StateTag tone={st.tone}>{st.text}</StateTag>
      </div>
      <div className={a.chips}>
        <span className={`${a.chip} ${a.chipRt}`}>
          Runtime // {runtimeLabel(e.runtime)}
        </span>
      </div>
      {e.present && e.bin ? (
        <p className={a.path}>{e.bin}</p>
      ) : (
        !retiring && (
          <div className={a.empty}>
            <b>Not installed on this host</b>
            No binary found for it. Its page lists where BattleLab looked.
          </div>
        )
      )}
      {retiring && (
        <p className={a.note}>
          {e.status_reason ||
            "This agent is being removed; its running sessions stay attachable until they exit."}{" "}
          Nothing new starts.
        </p>
      )}
      {e.kind !== "agent" && (
        <p className={a.plain}>Plain terminal, no agent behind it.</p>
      )}
      <ul className={a.chips} aria-label={`${e.label} capabilities`}>
        {CARD_CAPS.map(([k, label]) => {
          // A retiring engine starts nothing, whatever its manifest once declared.
          const on = !retiring && e.capabilities[k];
          return (
            <li key={k} className={on ? a.chip : `${a.chip} ${a.chipOff}`}>
              {label}
              {!on && <span className="sr-only"> (off)</span>}
            </li>
          );
        })}
      </ul>
      {row ? (
        <EngineMeter b={b} e={e} />
      ) : (
        e.kind !== "agent" && (
          <p className={a.meterNone}>
            <span>Usage</span>
            <span>— nothing to meter</span>
          </p>
        )
      )}
      <div className={a.acts}>
        <Link
          to={agentPath(e.id)}
          state={location.state}
          className={button.ghost}
          aria-label={`Details for ${e.label}`}
        >
          Details
        </Link>
      </div>
    </li>
  );
}

/** The plugin's directory name, from the manifest path the loader reported. */
function problemName(source: string): string {
  const parts = source.split("/").filter(Boolean);
  const i = parts.lastIndexOf("plugin.toml");
  return i > 0 ? parts[i - 1] : source;
}

/** A manifest that failed to load: its source and the loader's exact error. It is not an engine,
 *  so it offers nothing — not even a page of its own. */
function ProblemCard({ p }: { p: EngineProblem }) {
  return (
    <li className={`${a.card} ${a.bad}`}>
      <div className={a.ctop}>
        <span className={`${a.badge} ${a.badgeNone}`} aria-hidden="true">
          ??
        </span>
        <div className={a.name}>
          <h3>{problemName(p.source)}</h3>
        </div>
        <StateTag tone="down">Invalid manifest</StateTag>
      </div>
      <p className={a.err}>
        {p.source}
        <br />
        {p.error}
      </p>
      <p className={a.plain}>
        Not loaded. Nothing from this plugin runs until the manifest is fixed.
      </p>
    </li>
  );
}

export function AgentsRoster() {
  const roster = useEngineRoster();
  const location = useLocation();
  const b = useAgentBudgets();
  const [retrying, setRetrying] = useState(false);

  // Ask again on arrival: the roster provider loaded once at app start, and this is the page an
  // operator opens right after installing an agent. A failure keeps the list already shown.
  useEffect(() => {
    void refreshRoster();
  }, []);

  const retry = async () => {
    setRetrying(true);
    await refreshRoster();
    setRetrying(false);
  };

  const { engines, problems } = roster;
  const active = engines.filter(isActive).length;
  const retiring = engines.length - active;

  return (
    <section
      className={`${styles.section} ${a.wide}`}
      aria-labelledby="agents-h"
    >
      <div className={a.head}>
        <div className={a.headText}>
          <h2 id="agents-h">Agents</h2>
          <p className={a.summary}>
            Agents // <b>{active}</b> loaded · <b>{retiring}</b> retiring ·{" "}
            <b>{problems.length}</b> invalid
          </p>
          <p className={styles.hint}>
            Read from each plugin's manifest; nothing here is typed in by hand.
            Plan percentages come from the agent itself; the rest are counted
            against a limit you set.
          </p>
        </div>
        <Link
          to={settingsPath("agents-defaults")}
          state={location.state}
          className={a.headLink}
        >
          Defaults →
        </Link>
      </div>

      <BudgetBar b={b} />

      {roster.status === "failed" && (
        <div className={a.stale} role="alert">
          <span>
            {roster.loaded
              ? "Couldn’t refresh the agent list — showing the last one loaded."
              : "Couldn’t load the agent list."}
          </span>
          <button
            type="button"
            className={button.ghost}
            onClick={() => void retry()}
            disabled={retrying}
          >
            {retrying ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}
      {!roster.loaded && roster.status === "loading" && (
        <p className={styles.hint} role="status">
          Loading agents…
        </p>
      )}
      {roster.loaded && engines.length === 0 && problems.length === 0 && (
        <div className={a.empty} role="status">
          <b>No agents loaded</b>
          The server answered with an empty roster: no agent manifest loaded on
          this host.
        </div>
      )}

      {(engines.length > 0 || problems.length > 0) && (
        <ul className={a.grid} aria-label="Agents">
          {engines.map((e) => (
            <RosterCard key={e.id} e={e} b={b} />
          ))}
          {problems.map((p) => (
            <ProblemCard key={p.source} p={p} />
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- /settings/agents/:id — one agent's own page -------------------------------------------------------

function Kv({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className={a.kv}>
      {rows.map(([k, v]) => (
        <div key={k} className={a.kvRow}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** How the binary was found, in words. */
function provenanceNote(d: EngineDetail): string {
  const p = d.provenance;
  const via =
    p.via === "env"
      ? `set by ${d.binary?.env_var ?? "its environment override"}`
      : p.via === "search_paths"
        ? `found in its search paths (${d.binary?.search_paths.join(", ") ?? ""})`
        : p.via === "install"
          ? "installed by BattleLab"
          : p.state === "absent"
            ? "not found on this host"
            : "";
  return [via, p.note].filter(Boolean).join(" — ");
}

type DetailState =
  | { for: string; kind: "ok"; d: EngineDetail }
  | { for: string; kind: "missing" }
  | { for: string; kind: "failed" };

export function AgentDetail({ id }: { id: string }) {
  useEngineRoster();
  const location = useLocation();
  const b = useAgentBudgets();
  const [state, setState] = useState<DetailState | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    api
      .engineDetail(id)
      .then((d) => alive && setState({ for: id, kind: "ok", d }))
      .catch((e: unknown) => {
        if (!alive) return;
        setState(
          e instanceof ApiError && e.status === 404
            ? { for: id, kind: "missing" }
            : { for: id, kind: "failed" },
        );
      });
    return () => {
      alive = false;
    };
  }, [id, nonce]);

  const cur = state?.for === id ? state : null;
  const roster = settingsPath("agents");

  if (!cur) {
    return (
      <section className={`${styles.section} ${a.wide}`} aria-busy="true">
        <p className={styles.hint} role="status">
          Loading agent…
        </p>
      </section>
    );
  }
  if (cur.kind === "missing") {
    return (
      <section
        className={`${styles.section} ${a.wide}`}
        aria-labelledby="agent-missing-h"
      >
        <h2 id="agent-missing-h">No such agent</h2>
        <p className={styles.hint}>
          No agent called <code>{id}</code> is loaded on this host. It may have
          been removed, or the link is wrong.
        </p>
        <Link to={roster} state={location.state} className={button.ghost}>
          ← All agents
        </Link>
      </section>
    );
  }
  if (cur.kind === "failed") {
    return (
      <section
        className={`${styles.section} ${a.wide}`}
        aria-labelledby="agent-failed-h"
      >
        <h2 id="agent-failed-h">{engineLabel(id)}</h2>
        <div className={a.stale} role="alert">
          <span>Couldn’t load this agent’s details.</span>
          <button
            type="button"
            className={button.ghost}
            onClick={() => setNonce((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      </section>
    );
  }

  const d = cur.d;
  const info = engineInfo(id);
  const st = engineState(info);
  const row = b.byEngine.get(id);
  const caps = Object.entries(d.capabilities);

  return (
    <div className={a.detail} style={accent(id)}>
      <section className={`${styles.section} ${a.wide} ${a.hero}`}>
        <div className={a.head}>
          <div className={a.ctop}>
            <span className={a.badge} aria-hidden="true">
              {engineBadge(id)}
            </span>
            <div className={a.name}>
              <h2 className={a.heroTitle}>{d.label}</h2>
              <span>{d.id}</span>
            </div>
          </div>
          <div className={a.chips}>
            <StateTag tone={st.tone}>{st.text}</StateTag>
            <span className={`${a.chip} ${a.chipRt}`}>
              Runtime // {runtimeLabel(d.runtime)}
            </span>
            <span className={`${a.chip} ${a.chipRt}`}>{d.provenance.state}</span>
          </div>
        </div>
      </section>

      <div className={a.cols}>
        <section className={styles.section} aria-labelledby="agent-identity-h">
          <h2 id="agent-identity-h">Identity</h2>
          <Kv
            rows={[
              ["Id", d.id],
              ["Label", d.label],
              ["Publisher", d.publisher],
              ...(d.version ? [["Version", d.version] as [string, string]] : []),
              ["Manifest contract", String(d.contract)],
              ["Kind", d.kind],
              [
                "Source",
                <>
                  {d.source}
                  {d.source === "in-tree" && <small>Shipped with BattleLab.</small>}
                </>,
              ],
            ]}
          />
        </section>

        {d.runtime === "chat" && (
          // An API agent (#1209): where its conversations go. Before the binary section, which
          // for this runtime only says there is none.
          <section className={styles.section} aria-label="Endpoint">
            <AgentEndpointCard engine={d.id} />
          </section>
        )}

        <section className={styles.section} aria-labelledby="agent-binary-h">
          <h2 id="agent-binary-h">Binary &amp; provenance</h2>
          {d.binary ? (
            <Kv
              rows={[
              ["Resolved path", d.provenance.path ?? "— not found"],
              [
                "State",
                <>
                  {d.provenance.state}
                  {provenanceNote(d) && <small>{provenanceNote(d)}</small>}
                </>,
              ],
              ["Binary", d.binary.name],
              ["Env override", d.binary.env_var ?? "none"],
              [
                "Search paths",
                d.binary.search_paths.length
                  ? d.binary.search_paths.join(", ")
                  : "none declared",
              ],
            ]}
            />
          ) : (
            <div className={a.empty}>
              <b>No binary</b>
              This agent runs no process: BattleLab talks to its endpoint
              {d.endpoint ? ` (${d.endpoint.kind})` : ""} and runs nothing on this
              host.
            </div>
          )}
        </section>

        <section className={styles.section} aria-labelledby="agent-store-h">
          <h2 id="agent-store-h">Store</h2>
          {d.store ? (
            <Kv
              rows={[
                ["Path", d.store.root],
                ...(d.store.resolved && d.store.resolved !== d.store.root
                  ? [["Resolved", d.store.resolved] as [string, string]]
                  : []),
                ["Layout", d.store.layout],
                ["Access", d.store.read_only ? "read-only" : "read-write"],
              ]}
            />
          ) : (
            <div className={a.empty}>
              <b>No store</b>
              This agent keeps no session store BattleLab reads.
            </div>
          )}
        </section>

        <section className={styles.section} aria-labelledby="agent-kinds-h">
          <h2 id="agent-kinds-h">Kinds</h2>
          <Kv
            rows={[
              ...(d.launch
                ? ([
                    ["Resume", d.launch.resume],
                    ["New", d.launch.new ?? "not declared"],
                    ...(d.launch.admission
                      ? [["Admission", d.launch.admission] as [string, string]]
                      : []),
                  ] as [string, string][])
                : ([["Launch", "none — runs no process"]] as [string, string][])),
              [
                "Transcript",
                `${d.transcript.kind ?? "none"}${d.transcript.strict ? " · strict" : ""}`,
              ],
              [
                "Usage",
                `${d.usage.source}${d.usage.kind ? ` · ${d.usage.kind}` : ""}`,
              ],
            ]}
          />
        </section>

        <section className={styles.section} aria-labelledby="agent-caps-h">
          <h2 id="agent-caps-h">Capabilities</h2>
          <ul className={a.caps}>
            {caps.map(([k, on]) => (
              <li key={k} className={`${a.cap} ${on ? "" : a.capOff}`}>
                <span>{k}</span>
                <span className={on ? a.on : a.off}>{on ? "on" : "off"}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.section} aria-labelledby="agent-models-h">
          <h2 id="agent-models-h">Models</h2>
          {d.models.length ? (
            <ul className={a.models}>
              {d.models.map((m) => (
                <li key={m.id}>
                  <b>{m.id}</b>
                  {m.context_window != null && (
                    <small>{m.context_window.toLocaleString()} tokens</small>
                  )}
                  {m.aliases.length > 0 && (
                    <small>aka {m.aliases.join(", ")}</small>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <div className={a.empty}>
              <b>None declared</b>
              {d.model_select?.configured_elsewhere
                ? `${d.label} takes its model from its own configuration, so a new session offers “default” only.`
                : `This manifest lists no models, so no model picker is shown. ${d.label} picks its own.`}
            </div>
          )}
          {d.model_select?.supported && (
            <AddedModels
              engine={d.id}
              ids={d.model_select.offered
                .filter((m) => m.source === "operator")
                .map((m) => m.id)}
              onSaved={() => {
                setNonce((n) => n + 1);
                void refreshRoster();
              }}
            />
          )}
        </section>

        <section className={styles.section} aria-labelledby="agent-budget-h">
          <h2 id="agent-budget-h">Budget</h2>
          {row && info ? (
            <>
              <EngineMeter b={b} e={info} />
              {row.source === "plan" && (
                <p className={styles.hint}>
                  Plan usage is reported by the agent itself — there is no
                  limit to set here. The alert threshold is on the roster.
                </p>
              )}
            </>
          ) : (
            <p className={styles.hint}>
              {d.kind === "agent"
                ? "No usage reported for this agent yet."
                : "Nothing to meter — no agent behind it."}
            </p>
          )}
          <BudgetError b={b} />
        </section>

        <section className={styles.section} aria-labelledby="agent-defaults-h">
          <h2 id="agent-defaults-h">Defaults</h2>
          <div className={a.empty}>
            <b>Set in one place</b>
            The default agent and the permission-bypass default are global. A
            new session can still change either for itself.
          </div>
          <Link
            to={settingsPath("agents-defaults")}
            state={location.state}
            className={a.headLink}
          >
            Agents › Defaults →
          </Link>
        </section>
      </div>
    </div>
  );
}

/** The launch grammar (#1189), mirrored for an immediate hint. The server's check is the gate. */
const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}$/;
const ADDED_MODELS_MAX = 16;

/** Operator-added model ids for one engine (#1189): vendor churn without a release. Each save
 *  sends the engine's WHOLE list (the server replaces it), and a removed id stops being offered —
 *  a session form still holding it is refused at launch, never switched to `default`.
 *
 *  Because a save replaces the list, the list it is built from must be the one the LAST save
 *  stored, not the `ids` prop: the detail refetch a save triggers lands later, so an add then a
 *  remove built from the prop would erase the id just added. After our first save the list is
 *  local and authoritative (the save's answer, or what we sent when the answer carries no list),
 *  and edits are serialised — every control is disabled while a save is in flight. */
function AddedModels({
  engine,
  ids,
  onSaved,
}: {
  engine: string;
  ids: string[];
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // What our last successful save stored; `null` until then (the prop is all we know).
  const [stored, setStored] = useState<string[] | null>(null);
  const list = stored ?? ids;

  const save = async (next: string[]) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.setAgentDefaults({ models: { [engine]: next } });
      const got = res?.agent_defaults?.models?.[engine];
      setStored(Array.isArray(got) ? got : next);
      setDraft("");
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError && e.message ? e.message : "Couldn’t save the model list.");
    } finally {
      setBusy(false);
    }
  };

  const add = () => {
    const id = draft.trim();
    if (!MODEL_ID_RE.test(id) || id.toLowerCase() === "default") {
      setError(
        "A model id is letters, digits and . _ : / - only, and starts with a letter or digit.",
      );
      return;
    }
    if (list.includes(id)) {
      setError(`${id} is already on the list.`);
      return;
    }
    void save([...list, id]);
  };

  return (
    <div className={a.addedModels} data-testid="added-models">
      <h3 className={a.subhead}>Added by you</h3>
      {list.length ? (
        <ul className={a.models}>
          {list.map((id) => (
            <li key={id}>
              <b>{id}</b>
              <button
                type="button"
                className={button.ghost}
                disabled={busy}
                aria-label={`Remove ${id}`}
                onClick={() => void save(list.filter((x) => x !== id))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.hint}>
          None. Add a model id the vendor released after this build.
        </p>
      )}
      {list.length < ADDED_MODELS_MAX && (
        <form
          className={a.addModelRow}
          onSubmit={(e) => {
            e.preventDefault();
            add();
          }}
        >
          <input
            type="text"
            aria-label="Model id to add"
            placeholder="model id"
            value={draft}
            maxLength={96}
            disabled={busy}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            onChange={(e) => setDraft(e.target.value)}
          />
          <button type="submit" className={button.ghost} disabled={busy || !draft.trim()}>
            Add model
          </button>
        </form>
      )}
      {error && (
        <p className={styles.hint} role="alert" data-testid="added-models-error">
          {error}
        </p>
      )}
    </div>
  );
}

// ---- /settings/agents/defaults ---------------------------------------------------------------------

export function AgentDefaultsPage() {
  const config = useConfig();
  const refreshConfig = useConfigRefresh();
  const location = useLocation();
  const roster = useEngineRoster();
  // What a save of ours returned, shown until the config refresh it triggered lands — keyed on the
  // config OBJECT it was saved against, so it can never outlive that refresh (Hermes on #1163).
  const [saved, setSaved] = useState<{ d: AgentDefaults; cfg: typeof config }>();
  // The config as of the LATEST commit, read when a save's answer arrives (Hermes on #1163): an
  // earlier save's refresh can land while this POST is in flight, and binding the answer to the
  // config the POST started from would let that older refresh hide it. ConfigProvider applies
  // only the newest issued read, so once our own refresh is issued, no read issued before it
  // can land. A LAYOUT effect: it runs inside the commit, so an answer arriving between a config
  // commit and a passive effect still sees that config.
  const configRef = useRef(config);
  useLayoutEffect(() => {
    configRef.current = config;
  }, [config]);
  const current = saved && saved.cfg === config ? saved.d : config?.agent_defaults;
  const stored = current?.default_engine ?? null;
  const storedBypass = current?.bypass ?? true;

  // What the operator has picked on this page, or `undefined` for "untouched — show the stored
  // value". They are compared against the STORED values to decide what a save sends, so a save
  // carries only the fields that changed: a bypass-only save never re-sends the default engine,
  // which may name an agent that is not installed right now (the server keeps it, #1128).
  //
  // Draft ownership (Hermes on #1163). A draft belongs to an EPOCH, and an epoch ends whenever
  // the config changes for a reason that is not our own save — a Defaults save in another tab, or
  // any other panel's refresh. Ended is permanent: a stored value that goes A → B → A does not
  // revive a draft made against A, so it can never ride along in an unrelated save. Our own save
  // does not end the epoch: its fields are cleared as they are sent (the controls are disabled
  // while the POST is in flight, so nothing newer can be lost), and anything picked after it
  // resolves survives the refresh it triggered.
  const [epoch, setEpoch] = useState(0);
  const [seenConfig, setSeenConfig] = useState(config);
  // What our last save returned, until the next config change: that change is OURS only if it
  // carries exactly those defaults. A refresh that failed leaves no change to consume, so the
  // next change — someone else's — must not be mistaken for ours and keep old drafts alive.
  const [ownRefresh, setOwnRefresh] = useState<AgentDefaults | null>(null);
  if (config !== seenConfig) {
    // Adjusting state while rendering, React's pattern for "a prop changed" — no effect, no
    // extra paint with the stale draft.
    setSeenConfig(config);
    const got = config?.agent_defaults;
    const ours =
      ownRefresh !== null &&
      got?.default_engine === ownRefresh.default_engine &&
      got?.bypass === ownRefresh.bypass;
    setOwnRefresh(null);
    if (!ours) setEpoch((n) => n + 1);
  }
  const [engineEdit, setEngineEdit] = useState<{ v: string; epoch: number }>();
  const [bypassEdit, setBypassEdit] = useState<{ v: boolean; epoch: number }>();
  const engineDraft = engineEdit?.epoch === epoch ? engineEdit.v : undefined;
  const bypassDraft = bypassEdit?.epoch === epoch ? bypassEdit.v : undefined;
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const eligible = roster.engines.filter((e) => eligibleFor(e, "new"));
  const choice = resolveDefault(roster.engines, stored, "new");
  const picked = engineDraft ?? stored;
  const bypass = bypassDraft ?? storedBypass;
  const patch: { default_engine?: string; bypass?: boolean } = {};
  if (engineDraft !== undefined && engineDraft !== stored)
    patch.default_engine = engineDraft;
  if (bypassDraft !== undefined && bypassDraft !== storedBypass)
    patch.bypass = bypassDraft;
  const dirty = Object.keys(patch).length > 0;
  // The stored default, shown (greyed, unselectable) when it is not one the operator could pick.
  const gone =
    roster.loaded && stored && !eligible.some((e) => e.id === stored)
      ? stored
      : null;

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setMsg(null);
    const sent = patch;
    try {
      const r = await api.setAgentDefaults(sent);
      // The server's answer is authoritative until the refresh lands; the sent fields are done.
      setSaved({ d: r.agent_defaults, cfg: configRef.current });
      if ("default_engine" in sent) setEngineEdit(undefined);
      if ("bypass" in sent) setBypassEdit(undefined);
      setMsg({ ok: true, text: "Saved." });
      // Every `useConfig()` consumer — the new-session form above all — reads the new values.
      // The config change this causes is ours, so it does not end the drafts' epoch.
      setOwnRefresh(r.agent_defaults);
      refreshConfig();
    } catch (e) {
      setMsg({
        ok: false,
        text: e instanceof Error ? e.message : "Couldn’t save.",
      });
    } finally {
      setSaving(false);
    }
  };

  const option = (id: string, unavailable: boolean) => (
    <label
      key={id}
      className={`${a.opt} ${unavailable ? a.optGone : ""}`}
      style={accent(id)}
    >
      <input
        type="radio"
        name="default-engine"
        value={id}
        checked={picked === id}
        disabled={unavailable || saving}
        onChange={() => setEngineEdit({ v: id, epoch })}
      />
      <span className={`${a.badge} ${a.badgeSm}`} aria-hidden="true">
        {engineBadge(id)}
      </span>
      <span className={a.optLabel}>
        {engineLabel(id)}
        <small>
          {id}
          {unavailable &&
            (engineInfo(id)?.present ? " · not available" : " · not installed")}
        </small>
      </span>
    </label>
  );

  return (
    <section
      className={`${styles.section} ${a.wide}`}
      aria-labelledby="agent-defaults-page-h"
    >
      <div className={a.head}>
        <div className={a.headText}>
          <h2 id="agent-defaults-page-h">Defaults</h2>
          <p className={styles.hint}>
            What a new session starts with. Only installed agents can be
            picked.
          </p>
        </div>
        <Link
          to={settingsPath("agents")}
          state={location.state}
          className={a.headLink}
        >
          ← Roster
        </Link>
      </div>

      {roster.loaded && choice.unavailableDefault && (
        <div className={a.warn} role="status">
          <span className="hud-led attention" aria-hidden="true" />
          <div>
            <b>Default unavailable</b>
            {unavailableDefaultNotice(choice.unavailableDefault, choice.engine)}
          </div>
        </div>
      )}

      <div className={a.cols}>
        <fieldset className={a.pick}>
          <legend className={a.label}>Default agent for new sessions</legend>
          {!roster.loaded ? (
            <p className={styles.hint} role="status">
              Loading agents…
            </p>
          ) : (
            <>
              {gone && option(gone, true)}
              {eligible.map((e) => option(e.id, false))}
              {eligible.length === 0 && (
                <div className={a.empty}>
                  <b>No agent can start a session</b>
                  None of the loaded agents is installed and able to start a
                  new session.
                </div>
              )}
            </>
          )}
        </fieldset>

        <div className={a.side}>
          <p className={a.label} id="agent-perm-h">
            Permissions
          </p>
          <label className={a.tog}>
            <span className={a.togText}>
              <b>Permission bypass for new sessions and missions</b>
              <small>
                The starting value of the new-session form; each session can
                still change it. Mission launches use it as set when each agent
                starts — turning it off doesn&rsquo;t reach agents already
                running. Scheduled mission automations always keep bypass off.
              </small>
            </span>
            <span className={a.togState} aria-hidden="true">
              {bypass ? "on" : "off"}
            </span>
            <input
              type="checkbox"
              role="switch"
              className={a.switch}
              checked={bypass}
              disabled={saving}
              onChange={(e) =>
                setBypassEdit({ v: e.currentTarget.checked, epoch })
              }
            />
          </label>

          {roster.loaded && choice.engine && (
            <>
              <p className={a.label}>In effect now</p>
              <div className={a.opt} style={accent(choice.engine)}>
                <span className={`${a.badge} ${a.badgeSm}`} aria-hidden="true">
                  {engineBadge(choice.engine)}
                </span>
                <span className={a.optLabel}>
                  {engineLabel(choice.engine)}
                  <small>
                    {choice.engine}
                    {choice.engine !== stored &&
                      " · first engine eligible for new sessions"}
                  </small>
                </span>
              </div>
            </>
          )}

          <p className={a.label}>Where these apply</p>
          <dl className={a.where}>
            <div>
              <dt>New-session form</dt>
              <dd>preselected agent · bypass</dd>
            </div>
            <div>
              <dt>Mission launches</dt>
              <dd>bypass</dd>
            </div>
            <div>
              <dt>Handoff</dt>
              <dd>default target</dd>
            </div>
          </dl>

          <div className={a.saveRow}>
            <button
              type="button"
              className={button.primary}
              disabled={!dirty || saving}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : "Save defaults"}
            </button>
            {msg && (
              <span
                className={msg.ok ? a.saved : styles.budgetError}
                role={msg.ok ? "status" : "alert"}
              >
                {msg.text}
              </span>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
