/** The roster as one compact list (#1408): a row per CLI answering "is it working, and do I need
 *  to do anything?". Everything else — disable, remove, evidence, technical detail — lives on the
 *  agent's own page, reached from the row. The status of every row, chip, count and filter is the
 *  one `agentStatus` presenter's. */
import { useState, type CSSProperties } from "react";
import { Link, useLocation } from "react-router-dom";
import { engineBadge, engineColor, markRosterFailed, setRoster } from "../../app/engineRoster";
import type { AgentUsageRow } from "../../types/api";
import type { PluginCatalog } from "../../types/plugins";
import { agentPath } from "../../routes/settingsTabs";
import button from "../ui/actionButton.module.css";
import { pluginError, setupPath } from "../plugins/model";
import { api } from "../../lib/api";
import type { RosterRow, Status } from "./agentStatus";
import r from "./Roster.module.css";

const LED = { up: "up", idle: "idle", deg: "attention", down: "down" } as const;

/** A status: an LED and a word, never colour alone (docs/design.md §8). */
export function StatusWord({ status }: { status: Status }) {
  return (
    <span className={r.state} data-status={status.word}>
      <span className={`hud-led ${LED[status.tone]}`} aria-hidden="true" />
      {status.word}
    </span>
  );
}

function Usage({ row }: { row: AgentUsageRow | undefined }) {
  if (!row) return null;
  if (row.used_pct === null) {
    return <span>{row.source === "tokens" ? "tokens · no limit" : "—"}</span>;
  }
  const pct = Math.round(row.used_pct);
  const window = row.windows?.[0]?.label;
  return (
    <span className={r.state} title={`${pct}% used`}>
      <span className={r.meter} aria-hidden="true">
        <i style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </span>
      {pct}%{window ? ` · ${window}` : ""} {row.source}
    </span>
  );
}

function RowView({ row, usage }: { row: RosterRow; usage: Map<string, AgentUsageRow> }) {
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const accent = { "--eng": engineColor(row.id) } as CSSProperties;
  if (row.kind === "problem") {
    return (
      <li data-problem={row.problem.source}>
        <div className={r.row}>
          <span className={r.badge} aria-hidden="true">!</span>
          <span className={r.name}>
            <b>{row.label}</b>
            <small>Invalid agent definition</small>
          </span>
          <StatusWord status={row.status} />
          <span className={r.modes} />
          <span className={r.end}>
            <button
              type="button"
              className={`${button.ghost} ${r.problemToggle}`}
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "Hide details" : "Details"}
            </button>
          </span>
          <span className={r.chev} aria-hidden="true">{open ? "▾" : "▸"}</span>
        </div>
        {open && (
          <div className={r.diag} role="region" aria-label={`${row.label} manifest error`}>
            <p>{row.problem.source}</p>
            <p>{row.problem.error}</p>
            <p>Not loaded: nothing from this agent runs until the definition is fixed.</p>
            {/* Here, not on some agent's page: a roster of only invalid definitions has none. */}
            <ReloadRoster />
          </div>
        )}
      </li>
    );
  }
  const action = row.status.action;
  const firstUsage = row.kind === "agent" ? usage.get(row.id) : undefined;
  return (
    <li
      style={accent}
      {...(row.kind === "agent" ? { "data-engine": row.id } : { "data-plugin-id": row.id })}
    >
      <div className={r.row}>
        <span className={r.badge} aria-hidden="true">
          {row.kind === "agent" ? engineBadge(row.id) : row.label.slice(0, 2).toUpperCase()}
        </span>
        <Link
          className={r.name}
          to={agentPath(row.id)}
          state={location.state}
          aria-label={`Details for ${row.label}`}
        >
          <b>{row.label}</b>
          <small>{row.id}</small>
        </Link>
        <StatusWord status={row.status} />
        <span className={r.modes}>
          {row.kind === "agent" &&
            row.modes.map((m) => (
              <Link
                key={m.engine.id}
                className={r.mode}
                to={agentPath(m.engine.id)}
                state={location.state}
                aria-label={`${m.engine.label}: ${m.label}, ${m.status.word}`}
                data-mode={m.engine.id}
              >
                <span className={`hud-led ${LED[m.status.tone]}`} aria-hidden="true" />
                {m.label}
              </Link>
            ))}
        </span>
        <span className={r.end}>
          {action ? (
            <Link className={button.ghost} to={action.to}>
              {action.label}
            </Link>
          ) : (
            <Usage row={firstUsage} />
          )}
        </span>
        <span className={r.chev} aria-hidden="true">›</span>
      </div>
    </li>
  );
}

/** The installer's reload, then the roster's (the ⋯ menu's flow), for after a definition is fixed. */
function ReloadRoster() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const reload = async () => {
    setBusy(true);
    setError("");
    try {
      await api.pluginReload();
    } catch (e) {
      setError(pluginError(e));
      setBusy(false);
      return;
    }
    // The roster read is checked HERE, not through the shared helper that swallows a failure
    // (#1416 review): a failed read keeps the last rows, marks the roster failed (the page's
    // Retry), and says so on this row — the only recovery path an invalid-only roster has.
    try {
      const d = await api.engines();
      if (!Array.isArray(d?.engines)) throw new Error("not a roster");
      setRoster(d.engines, Array.isArray(d.problems) ? d.problems : []);
    } catch {
      markRosterFailed();
      setError("Reloaded, but couldn’t read the agent list — showing the last one. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" className={button.ghost} disabled={busy} onClick={() => void reload()}>
        {busy ? "Reloading…" : "Reload agent roster"}
      </button>
      {error && <p role="alert">{error}</p>}
    </>
  );
}

export function RosterList({
  rows,
  usage,
}: {
  rows: RosterRow[];
  usage: Map<string, AgentUsageRow>;
}) {
  return (
    <ul className={r.list} aria-label="All agents">
      {rows.map((row) => (
        <RowView key={row.id} row={row} usage={usage} />
      ))}
    </ul>
  );
}

/** Catalog agents that are not installed: one line, never rows (#1408). */
export function CatalogOnly({ entries }: { entries: PluginCatalog["catalog"] }) {
  if (!entries.length) return null;
  return (
    <p className={r.more} data-testid="catalog-only">
      Also in the catalog:{" "}
      {entries.map((c, i) => (
        <span key={c.manifest.identity.id} data-plugin-id={c.manifest.identity.id}>
          {/* Each name sets up THAT agent (the gallery's per-agent "Set up agent"), gated by the
              catalog's own installability — only installable entries reach this line. */}
          <Link
            to={setupPath({ plugin: c.manifest.identity.id })}
            aria-label={`Set up ${c.manifest.identity.label}`}
          >
            {c.manifest.identity.label}
          </Link>
          {i < entries.length - 1 ? "," : ""}
        </span>
      ))}
    </p>
  );
}
