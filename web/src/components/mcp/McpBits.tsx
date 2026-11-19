/** Library → MCP servers (#2163 P1c): the small pieces the index, the wizard and a server page share —
 *  a status LED with its words, the project squares, the "what happens next" list, the check report,
 *  and the review facts with the third-party caution. */
import { useState, type CSSProperties, type ReactNode } from "react";
import { Check, RefreshCw, ShieldAlert, X } from "lucide-react";

import type { McpCheckRow, McpReviewFacts } from "../../types/mcp";
import { stepLabel, urlHost, verdict, when, type Tone } from "./mcpModel";
import type { ProjectInfo } from "./mcpData";
import styles from "./mcp.module.css";

const LED: Record<Tone, string> = { up: "up", att: "att", down: "down", idle: "idle" };

export function StateWords({
  tone,
  words,
  testId,
  className = "",
}: {
  tone: Tone;
  words: ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <span className={`${styles.state} ${styles[tone] ?? ""} ${className}`.trim()} data-testid={testId} data-tone={tone}>
      <span className={`hud-led ${LED[tone]}`} aria-hidden="true" />
      <span>{words}</span>
    </span>
  );
}

/** One square per project: its own colour where the server is on, an outline where it is off. The
 *  words beside them carry the meaning; the squares only echo it. */
export function ProjectSquares({ projects, on }: { projects: ProjectInfo[]; on: (id: string) => boolean }) {
  if (!projects.length) return null;
  return (
    <span className={styles.squares} aria-hidden="true">
      {projects.slice(0, 12).map((p) => (
        <i key={p.id} className={on(p.id) ? undefined : styles.off} style={{ "--pc": p.color } as CSSProperties} />
      ))}
    </span>
  );
}

export function ProjectSquare({ color }: { color?: string }) {
  return <span className={styles.sq} aria-hidden="true" style={color ? ({ "--pc": color } as CSSProperties) : undefined} />;
}

const NEXT: [string, string][] = [
  ["Source", "where the server comes from. Nothing is fetched yet."],
  ["Review", "exactly what would run. Install happens only when you press Install."],
  ["Settings", "the values it needs. Credentials go to Variables."],
  ["Check", "start it, complete the handshake, list its tools."],
  ["Enable", "pick the projects whose sessions get it."],
];

export function WhatHappensNext({ current }: { current: number }) {
  return (
    <ol className={styles.next}>
      {NEXT.map(([b, t], i) => (
        <li key={b} className={i === current ? styles.cur : undefined} aria-current={i === current ? "step" : undefined}>
          <span>
            <b>{b}</b> — {t}
          </span>
        </li>
      ))}
    </ol>
  );
}

export function Caution({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className={styles.caution} role="note" data-testid="mcp-caution">
      <ShieldAlert size={18} aria-hidden="true" />
      <div>
        <b>{title}</b>
        {children}
      </div>
    </div>
  );
}

/** What a package review found: everything that decides trust, in one list. */
export function ReviewFacts({ facts }: { facts: McpReviewFacts }) {
  const s = facts.source;
  const pkg = s.kind === "remote" ? null : s;
  return (
    <dl className={styles.kv} data-testid="mcp-review-facts">
      {pkg && (
        <>
          <dt>Package</dt>
          <dd className={styles.mono}>{pkg.package}</dd>
          <dt>Exact version</dt>
          <dd className={styles.mono}>{pkg.version}</dd>
        </>
      )}
      <dt>Registry</dt>
      <dd>{facts.registry}</dd>
      <dt>Integrity</dt>
      <dd className={styles.mono}>{facts.integrity || "—"}</dd>
      <dt>Dependencies</dt>
      <dd>
        {facts.dependencies} · all pinned in a lock{s.kind === "pypi" ? " with --require-hashes" : " with their integrity"}
      </dd>
      <dt>Install scripts</dt>
      <dd>
        <StateWords tone="idle" words="Never run" />
        {facts.install_scripts_declared.length
          ? `— ${facts.install_scripts_declared.join(" / ")} are skipped`
          : "— none declared"}
      </dd>
      <dt>Command</dt>
      <dd className={styles.mono}>{facts.runs}</dd>
      {facts.entrypoints.length > 1 && (
        <>
          <dt>Entry points</dt>
          <dd className={styles.mono}>{facts.entrypoints.join(", ")}</dd>
        </>
      )}
      {facts.license && (
        <>
          <dt>License</dt>
          <dd>{facts.license}</dd>
        </>
      )}
      <dt>Lock</dt>
      <dd className={styles.mono}>{facts.lock_digest}</dd>
    </dl>
  );
}

export function RemoteFacts({ url }: { url: string }) {
  return (
    <dl className={styles.kv} data-testid="mcp-review-facts">
      <dt>URL</dt>
      <dd className={styles.mono}>{url}</dd>
      <dt>Host</dt>
      <dd className={styles.mono}>{urlHost(url)}</dd>
      <dt>Transport</dt>
      <dd>Streamable HTTP over HTTPS · certificate verified · no redirects to another host</dd>
      <dt>Install</dt>
      <dd>Nothing to install: sessions connect to this URL through BattleLab’s relay.</dd>
    </dl>
  );
}

/** One check's report: the verdict, each step with its LED and evidence, the tool list behind a
 *  toggle, and the actions the caller passes. */
export function CheckReport({
  row,
  scopeLabel,
  actions,
}: {
  row: McpCheckRow;
  scopeLabel: string;
  actions?: ReactNode;
}) {
  const [showTools, setShowTools] = useState(false);
  const v = verdict(row);
  const steps = row.steps.filter((s) => s.step !== "stderr");
  const stderr = row.steps.find((s) => s.step === "stderr");
  return (
    <div className={styles.face} data-testid="mcp-check-report" data-outcome={row.outcome}>
      <p className={`${styles.verdict} ${styles[v.tone] ?? ""}`} role="status">
        <span className={`hud-led ${v.tone}`} aria-hidden="true" />
        <b>{v.words}</b>
        <span>
          {row.outcome === "passed"
            ? `The server works with the ${scopeLabel}.`
            : v.sub}
        </span>
        <span className={styles.mono}>{when(row.at)}</span>
      </p>
      <ol className={styles.checks}>
        {steps.map((s, i) => {
          const tools = s.step === "tools" && s.ok && row.tools.length > 0;
          return (
            <li key={`${s.step}-${i}`} className={s.ok ? undefined : styles.bad} data-step={s.step}>
              <span className={`hud-led ${s.ok ? "up" : "down"}`} aria-hidden="true" />
              <span className={styles.n}>{String(i + 1).padStart(2, "0")}</span>
              <span className={styles.label}>{stepLabel(s.step)}</span>
              <span className={styles.what}>
                {s.ok ? s.detail : <span className={styles.detailBad}>{s.detail}</span>}
                {tools && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className={styles.linkBtn}
                      aria-expanded={showTools}
                      onClick={() => setShowTools((x) => !x)}
                    >
                      {showTools ? "Hide list" : "Show list"}
                    </button>
                    {showTools && (
                      <ul className={styles.toolList} aria-label="Tools">
                        {row.tools.map((t) => (
                          <li key={t}>{t}</li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
              </span>
              <span className={styles.res}>
                {s.ok ? <Check size={12} aria-hidden="true" /> : <X size={12} aria-hidden="true" />}{" "}
                {s.ok ? "ok" : "failed"}
              </span>
            </li>
          );
        })}
      </ol>
      {stderr && (
        <details>
          <summary className={styles.note}>The server’s own output (last lines)</summary>
          <span className={styles.detailBad}>{stderr.detail}</span>
        </details>
      )}
      {actions && <div className={styles.btnRow}>{actions}</div>}
    </div>
  );
}

export function RunAgainLabel({ busy, label = "Run check again" }: { busy: boolean; label?: string }) {
  return (
    <>
      <RefreshCw size={14} aria-hidden="true" />
      {busy ? "Checking…" : label}
    </>
  );
}
