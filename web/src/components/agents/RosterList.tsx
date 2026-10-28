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
import { versionsOf, type RosterRow, type Status, type Versions } from "./agentStatus";
import { reloadRoster } from "../../app/reloadRoster";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { Gauge } from "../hud/Meters";
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
      <span className={r.usageGauge}>
        <Gauge value={pct / 100} segments={14} low={-1} label={`${pct}% of the plan used`} />
      </span>
      {pct}%{window ? ` · ${window}` : ""} {row.source}
    </span>
  );
}

/** `installed → available`, or the current version and what Roll back would restore (#1741). */
function versionLine(v: Versions): string | null {
  if (!v.installed) return null;
  if (v.available) return `${v.installed} → ${v.available}`;
  return v.previous ? `${v.installed} active · ${v.previous.version ?? "previous"} kept` : `${v.installed} · current`;
}

/** Roll back (#1741): activate the generation that was active before, through the existing
 *  `activate` operation (the server re-checks its verification), then reload the roster. One
 *  confirmation; running sessions keep the binary they were started with. */
function RollBack({
  id,
  label,
  target,
  expected,
  onChanged,
}: {
  id: string;
  label: string;
  target: NonNullable<Versions["previous"]>;
  /** What the operator saw: the server refuses the rollback if either moved since. */
  expected: { active: string | null; revision: string | null };
  onChanged?: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const version = target.version ?? "the previous version";
  const confirm = async () => {
    setBusy(true);
    setError("");
    try {
      await api.pluginActivate({
        request_id: crypto.randomUUID(),
        plugin_id: id,
        generation_id: target.id,
        expected_active: expected.active,
        expected_revision: expected.revision,
      });
      await api.pluginReload();
      await reloadRoster();
      setAsking(false);
      onChanged?.();
    } catch (e) {
      setError(pluginError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" className={button.ghost} onClick={() => setAsking(true)}>
        Roll back to {version}
      </button>
      {asking && (
        <ConfirmDialog
          tag="ROLL BACK"
          title={`Roll back ${label} to ${version}?`}
          confirmLabel={`Roll back to ${version}`}
          busy={busy}
          onCancel={() => {
            setAsking(false);
            setError("");
          }}
          onConfirm={() => void confirm()}
        >
          <p>
            New sessions start on {version}. Running sessions keep the version they were started
            with until they restart. The current version stays installed.
          </p>
          {error && <p role="alert">{error}</p>}
        </ConfirmDialog>
      )}
    </>
  );
}

function RowView({
  row,
  usage,
  data,
  onChanged,
}: {
  row: RosterRow;
  usage: Map<string, AgentUsageRow>;
  data?: PluginCatalog | null;
  onChanged?: () => void;
}) {
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
  const versions = versionsOf(row.id, data ?? null);
  const line = versionLine(versions);
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
          {line && <small data-testid="agent-versions">{line}</small>}
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
          {action && (
            <Link
              className={button.ghost}
              to={action.to}
              aria-label={
                versions.available ? `${action.label} ${row.label} to ${versions.available}` : undefined
              }
            >
              {action.label}
            </Link>
          )}
          {/* Roll back stays offered beside a newer Update: the retained version is the way out
              of exactly the update that may be the problem (Hermes 7284). */}
          {versions.previous && (
            <RollBack
              id={row.id}
              label={row.label}
              target={versions.previous}
              expected={{
                active: data?.plugins.find((p) => p.id === row.id)?.active ?? null,
                revision: data?.roster_revision ?? null,
              }}
              onChanged={onChanged}
            />
          )}
          {/* Older history that cannot say which version came before (#1741): no one-click Roll
              back. Each kept version opens its own Enable step, which shows the agent's current
              state and confirms against it before anything is sent. */}
          {versions.undetermined.length > 0 && (
            <span className={r.undetermined} data-testid="rollback-undetermined">
              Previous version can’t be determined for older history. Choose a version:{" "}
              {versions.undetermined.map((g, i) => (
                <span key={g.id}>
                  <Link to={setupPath({ plugin: row.id, generation: g.id })} aria-label={`Choose ${row.label} ${g.version ?? g.id}`}>
                    {g.version ?? g.id}
                  </Link>
                  {i < versions.undetermined.length - 1 ? " · " : ""}
                </span>
              ))}
            </span>
          )}
          {!action && !versions.previous && versions.undetermined.length === 0 && <Usage row={firstUsage} />}
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
      setRoster(d.engines, Array.isArray(d.problems) ? d.problems : [], d.families ?? []);
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
  data,
  onChanged,
}: {
  rows: RosterRow[];
  usage: Map<string, AgentUsageRow>;
  /** The installer catalog: versions and Roll back (#1741). */
  data?: PluginCatalog | null;
  /** An installer change was made here (Roll back): re-read the catalog. */
  onChanged?: () => void;
}) {
  return (
    <ul className={r.list} aria-label="All agents">
      {rows.map((row) => (
        <RowView key={row.id} row={row} usage={usage} data={data} onChanged={onChanged} />
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
