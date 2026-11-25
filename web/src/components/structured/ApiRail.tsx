/** The API pane's side rail (#1726): what the agent is working from — its plan (a live checklist
 *  and the plan document it proposed) and its tools (MCP servers with their status, skills,
 *  commands, and the calls this conversation made).
 *
 *  It reads the normalized snapshot only (`native.plan`, `native.environment`, `native.counts`)
 *  and never names a client. Where it goes is the host's measured-width rule (`filePanelLayout`):
 *  docked beside the transcript when the pane can spare the column, otherwise a sheet over the
 *  pane — the same rule the Files drawer follows, whose dock slot it shares (one open at a time).
 *  The rail and each tab body scroll on their own, so a long plan or 256 skills stay reachable
 *  by pointer, touch and keyboard.
 *
 *  Status colour carries state only and always sits beside its word. A server only CONFIGURED
 *  (named by the client's configuration, nothing measured) gets no LED: a hollow mark and the
 *  word, so it never reads as healthy. */
import { useEffect, useLayoutEffect, useRef, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { Check, X } from "lucide-react";
import type {
  StructuredCounts,
  StructuredEnvironment,
  StructuredPlan,
  StructuredPlanEntry,
  StructuredSnapshot,
} from "../../types/api";
import { Disclosure, Tabs } from "../hud/Controls";
import { Callsign } from "../hud/Panel";
import { Markdown } from "./Markdown";
import styles from "./ApiRail.module.css";

export type RailTab = "plan" | "tools";

const STATE_WORD: Record<StructuredPlanEntry["status"], string> = {
  completed: "Done",
  in_progress: "In progress",
  pending: "Pending",
};

const MCP_WORD: Record<string, string> = {
  connected: "Connected",
  failed: "Failed",
  pending: "Pending",
  needs_auth: "Needs sign-in",
  disabled: "Disabled",
  configured: "Configured",
};

const MCP_TONE: Record<string, string | null> = {
  connected: styles.ledUp,
  failed: styles.ledDown,
  needs_auth: styles.ledAtt,
  pending: styles.ledIdle,
  disabled: styles.ledIdle,
  configured: null, // unmeasured: no LED at all
};

const clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const full = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

function When({ at, prefix }: { at: number | null | undefined; prefix?: string }) {
  if (typeof at !== "number" || !Number.isFinite(at)) return null;
  const date = new Date(at * 1000);
  if (!Number.isFinite(date.getTime())) return null;
  return (
    <time dateTime={date.toISOString()} title={full.format(date)}>
      {prefix}
      {clock.format(date)}
    </time>
  );
}

function SectionHead({ title, meta, right }: { title: string; meta?: ReactNode; right?: ReactNode }) {
  return <Callsign as="h3" className={styles.sectionHead} title={title} value={meta} meta={right} />;
}

function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className={styles.empty} data-testid="api-rail-empty">
      <p className={styles.emptyTitle}>{title}</p>
      <p>{children}</p>
    </div>
  );
}

function planCounts(plan: StructuredPlan | null | undefined) {
  const entries = plan?.entries ?? [];
  return { done: entries.filter((e) => e.status === "completed").length, total: entries.length };
}

function PlanTab({ plan }: { plan: StructuredPlan | null | undefined }) {
  if (!plan || (!plan.entries && !plan.document)) {
    return (
      <Empty title="No plan yet">
        The agent hasn’t shared one. It appears here when the agent writes a plan or a todo list.
      </Empty>
    );
  }
  const { done, total } = planCounts(plan);
  const olderDocument =
    !!plan.entries &&
    typeof plan.document_at === "number" &&
    typeof plan.entries_at === "number" &&
    plan.document_at < plan.entries_at;
  return (
    <div className={styles.plan}>
      {plan.entries && (
        <section aria-label="Checklist">
          <SectionHead
            title="Plan"
            meta={total ? `${done} of ${total} done` : "empty"}
          />
          {plan.explanation && <p className={styles.explanation}>{plan.explanation}</p>}
          {total === 0 ? (
            <p className={styles.quiet}>The agent cleared its checklist.</p>
          ) : (
            <ol className={styles.entries} data-testid="api-rail-plan">
              {plan.entries.map((entry, i) => (
                <li
                  key={i}
                  className={styles.entry}
                  data-status={entry.status}
                  data-testid="api-rail-plan-entry"
                >
                  <span className={styles.mark} aria-hidden="true">
                    {entry.status === "completed" ? (
                      <Check size={12} />
                    ) : entry.status === "in_progress" ? (
                      <span className={`${styles.led} ${styles.ledUp}`} />
                    ) : (
                      <span className={styles.hollow} />
                    )}
                  </span>
                  <span className={styles.entryText}>{entry.text}</span>
                  <span className={styles.entryState}>{STATE_WORD[entry.status]}</span>
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
      {plan.document && (
        <Disclosure
          className={`${styles.document}${olderDocument ? ` ${styles.older}` : ""}`}
          testId="api-rail-plan-document"
          summary={
            <span className={styles.docSummary}>
              <span>
                Plan document
                {/* Proposed before the newest checklist: never shown as the current plan. */}
                {olderDocument && (
                  <span className={styles.earlier} data-testid="api-rail-document-earlier">
                    {" "}
                    · From earlier in the session
                  </span>
                )}
              </span>
              <span className={styles.docMeta}>
                {plan.document.split("\n").length} lines
                {plan.document_at ? " · " : ""}
                <When at={plan.document_at} />
              </span>
            </span>
          }
        >
          <div className={styles.docBody}>
            <Markdown text={plan.document} />
          </div>
        </Disclosure>
      )}
    </div>
  );
}

function callsOf(rows: { name: string; calls: number }[] | undefined, name: string) {
  return rows?.find((r) => r.name === name)?.calls ?? 0;
}

function ToolsTab({
  environment,
  counts,
  live,
}: {
  environment: StructuredEnvironment | null | undefined;
  counts: StructuredCounts | undefined;
  live: boolean;
}) {
  const mcp = environment?.mcp;
  const skills = environment?.skills;
  const commands = environment?.commands;
  const used = counts?.skills ?? [];
  const usedNames = new Set(used.map((s) => s.name));
  const tools = counts?.tools ?? [];
  const toolMax = Math.max(1, ...tools.map((t) => t.calls));
  const toolTotal = tools.reduce((n, t) => n + t.calls, 0) + (counts?.tools_other ?? 0);
  const listed = new Set((mcp ?? []).map((m) => m.name));
  const earlierServers = (counts?.servers ?? []).filter((s) => !listed.has(s.name));
  // Skills that were used but are not (or no longer) listed still show, as used.
  const skillRows = [
    ...(skills ?? []).map((s) => s.name),
    ...used.map((s) => s.name).filter((n) => !(skills ?? []).some((s) => s.name === n)),
  ];
  return (
    <div className={styles.tools}>
      {!environment && (
        <Empty title={live ? "Not reported yet" : "Not running"}>
          {live
            ? "This client hasn’t reported its tools. Calls this session are counted below."
            : "The tools are what the running agent reports; they show while it runs. Calls this session are counted below."}
        </Empty>
      )}
      {environment && (
        <section aria-label="MCP servers" data-testid="api-rail-mcp">
          <SectionHead title="MCP servers" meta={mcp ? mcp.length : "—"} right="Calls" />
          {!mcp ? (
            <p className={styles.quiet}>Not reported by this client.</p>
          ) : mcp.length === 0 ? (
            <p className={styles.quiet}>None configured.</p>
          ) : (
            <ul className={styles.rows}>
              {mcp.map((server) => {
                // `configured` maps to NO LED (null); only an unknown status falls back to idle.
                const tone =
                  server.status in MCP_TONE ? MCP_TONE[server.status] : styles.ledIdle;
                const calls = callsOf(counts?.servers, server.name);
                return (
                  <li
                    key={server.name}
                    className={styles.row}
                    data-status={server.status}
                    data-testid="api-rail-mcp-server"
                  >
                    {tone ? (
                      <span className={`${styles.led} ${tone}`} aria-hidden="true" />
                    ) : (
                      <span className={styles.square} aria-hidden="true" />
                    )}
                    <span className={styles.name}>{server.name}</span>
                    <span className={styles.word} data-tone={server.status}>
                      {MCP_WORD[server.status] ?? server.status}
                    </span>
                    <span className={styles.num} aria-label={`${calls} calls this session`}>
                      {calls || "—"}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
      {((counts?.unattributed ?? 0) > 0 || (counts?.servers_other ?? 0) > 0 || earlierServers.length > 0) && (
        <ul className={styles.rows} data-testid="api-rail-mcp-extra">
          {/* Counted this conversation, but not (or no longer) reported by the live client: an
              earlier generation's server keeps its total (Hermes on #1811). No LED: nothing
              about it is measured now. */}
          {earlierServers.map((s) => (
            <li key={s.name} className={styles.row} data-testid="api-rail-mcp-earlier">
              <span className={styles.square} aria-hidden="true" />
              <span className={styles.name}>{s.name}</span>
              <span className={styles.word}>Called earlier in this session</span>
              <span className={styles.num}>{s.calls}</span>
            </li>
          ))}
          {(counts?.servers_other ?? 0) > 0 && (
            <li className={styles.row}>
              <span className={styles.square} aria-hidden="true" />
              <span className={styles.name}>Other servers</span>
              <span className={styles.word} />
              <span className={styles.num}>{counts?.servers_other}</span>
            </li>
          )}
          {(counts?.unattributed ?? 0) > 0 && (
            <li className={styles.row} data-testid="api-rail-unattributed">
              <span className={styles.square} aria-hidden="true" />
              <span className={styles.name}>Unattributed MCP calls</span>
              <span className={styles.word} />
              <span className={styles.num}>{counts?.unattributed}</span>
            </li>
          )}
        </ul>
      )}
      {(environment || used.length > 0 || (counts?.skills_other ?? 0) > 0) && (
        <section aria-label="Skills" data-testid="api-rail-skills">
          <SectionHead
            title="Skills"
            meta={`${skills ? `${skills.length} available · ` : ""}${used.length} used`}
          />
          {environment && environment.skill_use === false && (
            <p className={styles.quiet}>This client doesn’t report using a skill.</p>
          )}
          {!skills && environment && <p className={styles.quiet}>Not listed by this client.</p>}
          {skillRows.length > 0 && (
            <ul className={styles.rows}>
              {skillRows.map((name) => (
                <li key={name} className={styles.row} data-testid="api-rail-skill">
                  <span className={styles.name}>{name}</span>
                  {usedNames.has(name) && (
                    <span className={styles.stamp}>
                      <Check size={10} aria-hidden="true" /> Used this session ·{" "}
                      {callsOf(used, name)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {(counts?.skills_other ?? 0) > 0 && (
            <p className={styles.quiet}>+ {counts?.skills_other} uses of other skills</p>
          )}
        </section>
      )}
      {environment && (
        <section aria-label="Commands" data-testid="api-rail-commands">
          <SectionHead title="Commands" meta={commands ? commands.length : "—"} />
          {!commands ? (
            <p className={styles.quiet}>Not reported by this client.</p>
          ) : commands.length === 0 ? (
            <p className={styles.quiet}>None available.</p>
          ) : (
            <ul className={styles.commands}>
              {commands.map((c) => (
                <li key={c.name} className={styles.command}>
                  /{c.name}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      <section aria-label="Tools used this session" data-testid="api-rail-tool-counts">
        <SectionHead title="Tools used" meta={`${toolTotal} calls`} />
        {tools.length === 0 && !(counts?.tools_other ?? 0) ? (
          <p className={styles.quiet}>No tool calls yet this session.</p>
        ) : (
          <ul className={styles.rows}>
            {tools.map((t) => (
              <li key={t.name} className={styles.meterRow} data-testid="api-rail-tool-count">
                <span className={styles.name}>{t.name}</span>
                <span className={styles.meter} aria-hidden="true">
                  <i style={{ width: `${(t.calls / toolMax) * 100}%` }} />
                </span>
                <span className={styles.num}>{t.calls}</span>
              </li>
            ))}
            {(counts?.tools_other ?? 0) > 0 && (
              <li className={styles.meterRow}>
                <span className={styles.name}>Other tools</span>
                <span className={styles.meter} aria-hidden="true" />
                <span className={styles.num}>{counts?.tools_other}</span>
              </li>
            )}
          </ul>
        )}
      </section>
    </div>
  );
}

export function ApiRail({
  snap,
  mode,
  width,
  tab,
  onTab,
  onClose,
}: {
  snap: StructuredSnapshot | null;
  mode: "dock" | "sheet";
  width: number;
  tab: RailTab;
  onTab: (tab: RailTab) => void;
  onClose: () => void;
}) {
  const native = snap?.native;
  const plan = native?.plan;
  const environment = native?.environment;
  const counts = native?.counts;
  const rootRef = useRef<HTMLDivElement>(null);
  const { done, total } = planCounts(plan);
  const toolCount =
    (environment?.mcp?.length ?? 0) + (environment?.skills?.length ?? 0) + (environment?.commands?.length ?? 0);
  // A sheet is modal over its pane: focus moves into it, Tab stays in it, Escape closes it (the
  // pane hands focus back to the control that opened it).
  // A modal sheet makes the rest of its pane inert while it is open — not a Tab stop, not a
  // pointer target, not read — and gives it back exactly as it was (Hermes on #1811).
  const wrapRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const wrap = wrapRef.current;
    const host = wrap?.parentElement;
    if (mode !== "sheet" || !wrap || !host) return;
    const changed = [...host.children].filter(
      (el): el is HTMLElement => el !== wrap && el instanceof HTMLElement && !el.inert,
    );
    for (const el of changed) el.inert = true;
    return () => {
      for (const el of changed) el.inert = false;
    };
  }, [mode]);
  useEffect(() => {
    if (mode !== "sheet") return;
    rootRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
  }, [mode]);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (mode !== "sheet" || e.key !== "Tab") return;
    const focusable = [
      ...(rootRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], [tabindex]',
      ) ?? []),
      // Only real Tab stops: a roving tab list's inactive tab (tabIndex -1) is not one, and
      // counting it let Shift+Tab out of the modal sheet (Hermes on #1811).
    ].filter((el) => el.tabIndex >= 0 && !el.closest("[inert]") && el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  const updated = Math.max(plan?.entries_at ?? 0, plan?.document_at ?? 0) || null;
  const rail = (
    <div
      ref={rootRef}
      className={`${styles.rail} ${mode === "sheet" ? styles.sheet : styles.dock}`}
      data-testid="api-rail"
      data-mode={mode}
      role={mode === "sheet" ? "dialog" : "complementary"}
      aria-modal={mode === "sheet" ? true : undefined}
      aria-label="Plan and tools"
      style={mode === "dock" ? ({ "--rail-w": `${width}px` } as CSSProperties) : undefined}
      onKeyDown={onKey}
    >
      {mode === "sheet" && <span className={styles.grip} aria-hidden="true" />}
      <div className={styles.head}>
        <Tabs<RailTab>
          className={styles.tabs}
          label="Plan and tools"
          value={tab}
          onChange={onTab}
          tabs={[
            { id: "plan", label: total ? `Plan ${done}/${total}` : "Plan" },
            { id: "tools", label: toolCount ? `Tools ${toolCount}` : "Tools" },
          ]}
        />
        <button
          type="button"
          className={styles.close}
          aria-label="Close plan and tools"
          title="Close"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      <div
        className={styles.body}
        data-testid="api-rail-body"
        role="tabpanel"
        aria-label={tab === "plan" ? "Plan" : "Tools"}
        tabIndex={0}
      >
        {tab === "plan" ? (
          <PlanTab plan={plan} />
        ) : (
          <ToolsTab environment={environment} counts={counts} live={!!native?.worker} />
        )}
      </div>
      <div className={styles.foot}>
        {tab === "plan" ? (
          <>
            <span>{updated ? <When at={updated} prefix="Updated " /> : "No plan shared"}</span>
            <span data-testid="api-rail-plan-state">
              {/* A checklist the client said it can no longer reconcile, or one an earlier
                  generation wrote that the live one has not re-confirmed (review on #1811). */}
              {plan?.stale ? "Saved — may be out of date" : native?.worker ? "Live" : "Saved"}
            </span>
          </>
        ) : (
          <>
            <span>{environment ? "Reported by the running client" : "Not reported"}</span>
            <span>Calls: this session</span>
          </>
        )}
      </div>
    </div>
  );
  if (mode === "dock") return rail;
  return (
    <div ref={wrapRef} className={styles.scrimWrap} data-testid="api-rail-sheet">
      <div className={styles.scrim} aria-hidden="true" onClick={onClose} />
      {rail}
    </div>
  );
}
