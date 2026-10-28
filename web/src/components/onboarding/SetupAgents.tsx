/** The setup wizard's Agents step (#1739): every agent the roster and the catalog know, each with
 *  its status, the client a new session of it will use, and its one action, run INLINE with the
 *  Add-agent flow (`AgentSetupFlow`, the same component as Settings → Agents → Add agent).
 *
 *  Status is the shared presenter's (`agentStatus`, #1408), so setup and Settings cannot disagree.
 *  Grouping an API client with its console agent reads the API client's manifest (`api.source`);
 *  nothing here names an engine. */
import { useCallback, useState } from "react";
import { engineName, type RosterSnapshot } from "../../app/engineRoster";
import type { PluginCatalog } from "../../types/plugins";
import { buildRows, catalogOnly, READY_RANK, savedStatus, versionsOf, type RosterRow, type Status } from "../agents/agentStatus";
import { pendingOperation } from "../plugins/model";
import { StatusWord } from "../agents/RosterList";
import { AgentSetupFlow } from "../plugins/AgentSetupFlow";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import button from "../ui/actionButton.module.css";
import s from "./SetupAgents.module.css";

type Client = "default" | "available" | "terminal" | "chat";

interface Row {
  id: string;
  name: string;
  sub: string;
  client: Client;
  status: Status;
  /** The inline action: its label and the setup query it opens the flow with. */
  action: { label: string; query: string } | null;
}

const NOT_INSTALLED: Status = {
  rank: 6,
  word: "Not installed",
  tone: "idle",
  action: null,
  reason: null,
};

const UNAVAILABLE: Status = {
  rank: 6,
  word: "Unavailable",
  tone: "idle",
  action: null,
  reason: null,
};

const CHIP: Record<Client, string> = {
  default: "API · default",
  available: "API available",
  terminal: "Terminal only",
  chat: "Chat",
};

/** The agent a setup query is about: its `plugin`, else its operation's (a row can carry its API
 *  client's operation, #1408 grouping), else the row's own. The embedded flow is locked to it. */
function pluginOf(query: string, catalog: PluginCatalog | null, fallback: string): string {
  const q = new URLSearchParams(query);
  const op = q.get("operation");
  return (
    q.get("plugin") ??
    (op ? catalog?.operations.find((o) => o.id === op)?.plugin_id : undefined) ??
    fallback
  );
}

/** `installed → available` for an installer-managed agent (#1741), as Settings → Agents says it. */
function versionText(id: string, catalog: PluginCatalog | null): string | null {
  const v = versionsOf(id, catalog);
  return v.installed ? (v.available ? `${v.installed} → ${v.available}` : v.installed) : null;
}

/** The query part of a `setupPath(...)` link, which the flow opens with. */
const queryOf = (to: string) => (to.includes("?") ? to.slice(to.indexOf("?")) : "");

function rowsOf(roster: RosterSnapshot, catalog: PluginCatalog | null): Row[] {
  const catalogApi = (id: string) =>
    !!catalog?.catalog.some((c) => c.manifest.api?.source === id && c.manifest.identity.id !== id);
  const entry = (id: string) =>
    catalog?.catalog.find((c) => c.manifest.identity.id === id && c.installable !== false);
  const out: Row[] = [];
  const live = roster.engines.filter((e) => e.kind !== "terminal");
  for (const row of buildRows(live, [], catalog) as RosterRow[]) {
    if (row.kind === "problem") continue;
    if (row.kind === "saved") {
      out.push({
        id: row.id,
        name: row.label,
        sub: [versionText(row.id, catalog), row.status.reason ?? "Saved installation"].filter(Boolean).join(" · "),
        client: catalogApi(row.id) ? "available" : "terminal",
        status: row.status,
        action: row.status.action
          ? { label: row.status.action.label, query: queryOf(row.status.action.to) }
          : null,
      });
      continue;
    }
    const own = row.modes[0].engine;
    const apiMode = row.modes.find((m) => m.engine.runtime === "api" && m.engine.id !== row.id);
    // The client a new session of this agent uses: its API client when that is ready (#1739). An
    // API client listed on its own (its console agent is not in the roster) is that client.
    const api = own.runtime === "api" ? row.modes[0] : apiMode;
    const client: Client = own.runtime === "chat"
      ? "chat"
      : api
        ? api.status.rank === READY_RANK
          ? "default"
          : "available"
        : catalogApi(row.id)
          ? "available"
          : "terminal";
    // An agent the roster knows but this host lacks, which the catalog can install.
    const installable = !own.present && own.runtime !== "api" && !!entry(row.id);
    const action = row.status.action
      ? { label: row.status.action.label, query: queryOf(row.status.action.to) }
      : installable
        ? { label: "Install", query: `?${new URLSearchParams({ plugin: row.id })}` }
        : null;
    const facts = [row.label, versionText(row.id, catalog), own.present ? own.bin : null].filter(Boolean).join(" · ");
    const why = apiMode && client === "available" ? apiMode.status.reason : row.status.reason;
    out.push({
      id: row.id,
      name: engineName(row.id),
      sub: why ? `${facts} · ${apiMode && client === "available" ? "API client: " : ""}${why}` : facts,
      client,
      status: row.status,
      action,
    });
  }
  // Every catalog agent is a row (#1739). One neither live nor saved is Not installed with Install;
  // one the catalog cannot install here is Unavailable with its reason. An API client is grouped
  // with its console agent (its chip says so) unless that agent is nowhere: then it is a row too.
  const shown = new Set(out.map((r) => r.id));
  const installable = new Set(catalogOnly(roster.engines, catalog).map((c) => c.manifest.identity.id));
  const known = new Set([...roster.engines.map((e) => e.id), ...(catalog?.catalog ?? []).map((c) => c.manifest.identity.id)]);
  for (const c of catalog?.catalog ?? []) {
    const id = c.manifest.identity.id;
    if (shown.has(id) || roster.engines.some((e) => e.id === id)) continue;
    const source = c.manifest.api?.source;
    if (source && source !== id && known.has(source)) continue;
    const can = installable.has(id);
    // An install the server still holds (no installation row yet): Setting up · View operation,
    // which reads the operation back by id — never a second Install (#1739 outcomes).
    const running = catalog?.operations.some((o) => o.plugin_id === id && pendingOperation(o));
    const status = running
      ? savedStatus(id, catalog)
      : can
        ? NOT_INSTALLED
        : { ...UNAVAILABLE, reason: c.reason ?? null };
    out.push({
      id,
      name: c.manifest.identity.label,
      sub: `${c.manifest.identity.publisher} · catalog ${c.manifest.identity.version}`,
      client: source ? "available" : catalogApi(id) ? "available" : "terminal",
      status,
      action: status.action
        ? { label: status.action.label, query: queryOf(status.action.to) }
        : can
          ? { label: "Install", query: `?${new URLSearchParams({ plugin: id })}` }
          : null,
    });
    if (!can && c.reason) out[out.length - 1].sub += ` · ${c.reason}`;
  }
  return out;
}

export function SetupAgents({
  roster,
  catalog,
  catalogError,
  onChanged,
  onSigninLive,
}: {
  roster: RosterSnapshot;
  catalog: PluginCatalog | null;
  /** Why the catalog could not be read, or null. Without it no install is offered. */
  catalogError: string | null;
  /** The flow finished or was closed: re-read the catalog (the roster reloads on enable). */
  onChanged: () => void;
  /** Whether an inline sign-in terminal is live — the wizard confirms before it closes one. */
  onSigninLive?: (live: boolean) => void;
}) {
  const [open, setOpen] = useState<{ id: string; query: string; plugin: string } | null>(null);
  // A live sign-in terminal dies with its flow, and with it the vendor's sign-in: every way of
  // closing the flow from here asks first (the page route's leave guard, embedded).
  const [live, setLive] = useState(false);
  const [pending, setPending] = useState<(() => void) | null>(null);
  const reportLive = useCallback(
    (value: boolean) => {
      setLive(value);
      onSigninLive?.(value);
    },
    [onSigninLive],
  );
  const guarded = (action: () => void) => (live ? setPending(() => action) : action());
  if (!roster.loaded) {
    return (
      <p className={s.wait} role="status">
        {roster.status === "failed"
          ? "Couldn’t load the agent list — retrying."
          : "Scanning…"}
      </p>
    );
  }
  const rows = rowsOf(roster, catalog);
  const close = () => {
    setOpen(null);
    onChanged();
  };
  const confirm = pending && (
    <ConfirmDialog
      tag="SIGN-IN"
      title="Leave sign-in?"
      confirmLabel="Leave sign-in"
      cancelLabel="Stay"
      role="alertdialog"
      onCancel={() => setPending(null)}
      onConfirm={() => {
        const action = pending;
        setPending(null);
        action();
      }}
    >
      <p>Leaving closes this temporary terminal and interrupts sign-in. You can start another attempt from the agent’s row.</p>
    </ConfirmDialog>
  );
  return (
    <>
      {catalogError && (
        <p className={s.note} role="alert">
          Couldn’t load the agent catalog ({catalogError}). Agents already on this host are listed;
          installing waits until the catalog loads.
        </p>
      )}
      <ul className={s.rows} aria-label="Agents">
        {rows.map((r) => {
          const expanded = open?.id === r.id;
          return (
            <li key={r.id} className={s.item} data-agent={r.id}>
              <div className={s.row}>
                <div className={s.who}>
                  <span className={s.name}>{r.name}</span>
                  <span className={`${s.chip} ${r.client === "default" ? s.chipDefault : ""}`}>
                    {CHIP[r.client]}
                  </span>
                  {r.sub && <span className={s.sub}>{r.sub}</span>}
                </div>
                <div className={s.end}>
                  <StatusWord status={r.status} />
                  {/* A catalog that failed to (re)load offers nothing to act on: its rows may be
                      stale, so the statuses stay and the actions wait for a good read. */}
                  {r.action && !expanded && !catalogError && (
                    <button
                      type="button"
                      className={r.status === NOT_INSTALLED ? button.ghost : button.primary}
                      aria-label={`${r.action.label}: ${r.name}`}
                      onClick={() => {
                        const query = r.action!.query;
                        const next = { id: r.id, query, plugin: pluginOf(query, catalog, r.id) };
                        guarded(() => setOpen(next));
                      }}
                    >
                      {r.action.label}
                    </button>
                  )}
                </div>
              </div>
              {expanded && (
                <div className={s.flow} role="region" aria-label={`Set up ${r.name}`}>
                  <AgentSetupFlow
                    plugin={open.plugin}
                    query={open.query}
                    onDone={close}
                    onCancel={() => guarded(close)}
                    onSigninLive={reportLive}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {confirm}
    </>
  );
}
