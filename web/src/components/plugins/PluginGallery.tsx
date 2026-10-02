import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { reloadRoster } from "../../app/reloadRoster";
import { agentPath } from "../../routes/settingsTabs";
import { api } from "../../lib/api";
import type { PluginCatalog, PluginInstallation, PluginManifest } from "../../types/plugins";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { usePluginCatalog, type GalleryFilter } from "./usePluginCatalog";
import { pendingOperation, pluginError, missingOperation, rejectedOperation, setupPath, sourceLabel } from "./model";
import button from "../ui/actionButton.module.css";
import s from "./PluginSetup.module.css";
import a from "../../routes/AgentsSettings.module.css";


export function GalleryToolbar({ catalog, query, setQuery, filter, setFilter }: {
  catalog: ReturnType<typeof usePluginCatalog>; query: string; setQuery: (s: string) => void;
  filter: GalleryFilter; setFilter: (f: GalleryFilter) => void;
}) {
  const { data, error, busy, load, refresh } = catalog;
  return <div className={s.catalogBar}>
    <div className={s.row}><span>{data?.feed.state === "ready" ? `Signed catalog · sequence ${data.feed.sequence}` : "Catalog unavailable"}</span>
      <button className={button.ghost} disabled={busy} onClick={() => void refresh()}>{busy ? "Refreshing…" : "Refresh catalog"}</button>
      <Link className={button.primary} to={setupPath()}>Add agent</Link>
    </div>
    {(error || data?.feed.error) && <div className={s.note} role="alert">{error || data?.feed.error}<button className={button.ghost} onClick={() => void load()}>Retry loading</button></div>}
    <label className={s.field}><span>Search agents</span><input className={s.input} type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Name or ID" /></label>
    <div className={s.row} aria-label="Agent filters">{(["All", "Ready", "Needs setup", "Updates", "Disabled"] as const).map(value =>
      <button key={value} className={`${button.ghost} ${s.filter}`} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value}</button>)}</div>
  </div>;
}

export function PluginActions({ id, label, data, onChange, children }: {
  id: string; label: string; data: PluginCatalog | null; onChange: () => Promise<void>; children?: ReactNode;
}) {
  const row = data?.plugins.find(p => p.id === id);
  const candidate = row?.generations?.find(g => g.id === (row.candidate ?? row.active));
  const entry = data?.catalog.find(c => c.manifest.identity.id === id);
  const operation = data?.operations.filter(o => o.plugin_id === id && pendingOperation(o)).sort((a, b) => b.created_at - a.created_at)[0];
  const [confirm, setConfirm] = useState<{ kind: "disable" | "remove"; trigger: HTMLElement; active: string | null; revision: string | null } | null>(null);
  const attemptKey = `plugin-deactivate:${id}`;
  type Attempt = { request_id: string; plugin_id: string; kind: "disable" | "remove"; expected_active?: string | null; expected_revision?: string | null };
  const [attempt, setAttempt] = useState<Attempt | null>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(attemptKey) ?? "null") as Attempt | null;
      return saved?.plugin_id === id && typeof saved.request_id === "string" && ["disable", "remove"].includes(saved.kind) ? saved : null;
    } catch { return null; }
  });
  const remember = (value: Attempt | null) => {
    if (value) sessionStorage.setItem(attemptKey, JSON.stringify(value));
    else sessionStorage.removeItem(attemptKey);
    setAttempt(value);
  };
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const forgetAbsent = async (message: string) => {
    await reloadRoster(); await onChange();
    remember(null); setConfirm(null);
    setError(`${message} Review the current installation and confirm a new decision.`);
  };
  const deactivate = async () => {
    if (!confirm || busy) return;
    setBusy(true); setError("");
    try {
      const current = attempt ?? { request_id: crypto.randomUUID(), plugin_id: id, kind: confirm.kind, expected_active: confirm.active, expected_revision: confirm.revision };
      let settled = false;
      if (attempt) {
        try {
          settled = (await api.pluginOperation(attempt.request_id)).state === "complete";
          if (!settled) throw new Error("The previous operation has not settled. Check its status again.");
        }
        catch (lookup) {
          if (!missingOperation(lookup)) throw lookup;
          await forgetAbsent("The previous request was not recorded.");
          return;
        }
      }
      if (!settled) {
        remember(current); // Persist before POST; closing/reloading the dialog retains this ID.
        const body = { request_id: current.request_id, plugin_id: id, expected_active: current.expected_active ?? null, expected_revision: current.expected_revision ?? null };
        try { await (current.kind === "remove" ? api.pluginRemove(body) : api.pluginDisable(body)); }
        catch (error) {
          if (rejectedOperation(error)) {
            try { settled = (await api.pluginOperation(current.request_id)).state === "complete"; }
            catch (lookup) {
              if (missingOperation(lookup)) {
                await forgetAbsent(pluginError(error));
                return;
              }
            }
          }
          if (!settled) throw error;
        }
      }
      await reloadRoster(); await onChange(); remember(null); setConfirm(null);
    } catch (e) { setError(pluginError(e)); } finally { setBusy(false); }
  };
  const active = row?.generations?.find(g => g.id === row.active);
  const update = !!entry && !!active && entry.digest !== active.review.recipe_digest;
  return <>
    {candidate && <p className={a.note}>{sourceLabel(candidate.review)} · {candidate.id === row?.active && row.enabled ? "Enabled" : "Disabled"}</p>}
    {row?.error && <p role="alert" className={s.note}>{row.error}</p>}
    <div className={`${a.acts} ${s.actions}`}>
      {children}
      {attempt && <button className={button.ghost} onClick={e => setConfirm({ kind: attempt.kind, trigger: e.currentTarget, active: row?.active ?? null, revision: data?.roster_revision ?? null })}>Check previous {attempt.kind}</button>}
      {operation ? <Link className={button.primary} to={setupPath({ operation: operation.id })}>View operation</Link>
        : candidate && (!row?.enabled || row.candidate !== row.active) ? <Link className={button.primary} to={setupPath({ plugin: id, generation: candidate.id })}>Continue setup</Link>
        : entry && !row?.enabled ? <Link className={button.primary} to={setupPath({ plugin: id })}>Set up agent</Link> : null}
      {update && <Link className={button.ghost} to={setupPath({ plugin: id })}>Review update</Link>}
      {row?.enabled && <button className={button.ghost} onClick={e => setConfirm({ kind: attempt?.kind ?? "disable", trigger: e.currentTarget, active: row.active ?? null, revision: data?.roster_revision ?? null })}>Disable</button>}
      {row && <button className={button.ghost} onClick={e => setConfirm({ kind: attempt?.kind ?? "remove", trigger: e.currentTarget, active: row.active ?? null, revision: data?.roster_revision ?? null })}>Remove</button>}
    </div>
    {error && !confirm && <p role="alert">{error}</p>}
    {confirm && <ConfirmDialog tag="AGENT INSTALLATION" title={`${confirm.kind === "remove" ? "Remove" : "Disable"} ${label}?`}
      confirmLabel={confirm.kind === "remove" ? "Remove agent" : "Disable agent"} busy={busy}
      returnFocusTo={confirm.trigger} onCancel={() => { setConfirm(null); setError(""); }} onConfirm={() => void deactivate()}>
      <p>New sessions will be unavailable. Running sessions stay attachable until they exit.</p>
      <p>Vendor transcripts, credentials and installed copies stay on this host. Saved defaults and budgets are retained.</p>
      {error && <p role="alert">{error}</p>}
    </ConfirmDialog>}
  </>;
}

/** Catalog/candidate cards are installer data, never fabricated members of the live roster. */
export function CatalogCard({ manifest, row, data, onChange }: {
  manifest: PluginManifest; row?: PluginInstallation; data: PluginCatalog; onChange: () => Promise<void>;
}) {
  const { id, label, version, publisher } = manifest.identity;
  return <li className={a.card} data-plugin-id={id}>
    <h3>{label}</h3><p>{version} · {publisher}</p>
    <p className={a.note}>{row ? "Installation saved; not in the live roster." : "Available in the signed catalog."}</p>
    <PluginActions id={id} label={label} data={data} onChange={onChange}>
      <Link className={button.ghost} to={agentPath(id)} aria-label={`Details for ${label}`}>Details</Link>
    </PluginActions>
  </li>;
}


/** Installation state is separate from live engine identity, including disabled candidates. */
export function PluginInstallationDetail({ id, label, catalog, onChange }: {
  id: string; label: string; catalog: ReturnType<typeof usePluginCatalog>; onChange: () => Promise<void>;
}) {
  const { data, error, busy, refresh } = catalog;
  const row = data?.plugins.find(p => p.id === id);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState("");
  const reload = async () => {
    setReloading(true); setReloadError("");
    try { await api.pluginReload(); await reloadRoster(); await onChange(); }
    catch (error) { setReloadError(pluginError(error)); }
    finally { setReloading(false); }
  };
  const generations = row?.generations?.filter(g => g.id === row.active || g.id === row.candidate) ?? [];
  return <section className={`${s.panel} ${s.stack}`} aria-label="Installation management">
    <h2>Installation</h2>
    {!data && !error && <p role="status">Loading installation…</p>}
    <div className={s.row}>
      <span>{data?.feed.state === "ready" ? `Signed catalog · sequence ${data.feed.sequence}` : "Catalog unavailable"}</span>
      {data?.feed.expires_at && <span>Valid until {new Date(data.feed.expires_at * 1000).toLocaleString()}</span>}
      <button className={button.ghost} disabled={busy} onClick={() => void refresh()}>{busy ? "Refreshing…" : "Refresh catalog"}</button>
      <button className={button.ghost} disabled={reloading} onClick={() => void reload()}>{reloading ? "Reloading…" : "Reload agent roster"}</button>
    </div>
    {(error || data?.feed.error || reloadError) && <p role="alert">{error || data?.feed.error || reloadError}</p>}
    {data && !row && <p>No managed installation is saved for this agent.</p>}
    {generations.map(gen => <section className={s.panel} key={gen.id} aria-label={gen.id === row?.active ? "Active installation" : "Candidate installation"}>
      <h3>{gen.id === row?.active ? "Active installation" : "Candidate installation"} · {gen.review.entry.manifest.identity.version}</h3>
      <p>{sourceLabel(gen.review)} · {gen.review.entry.manifest.identity.publisher}</p>
      <p>{gen.id === row?.active && row.enabled ? "Enabled" : "Disabled"}</p>
      <p>{gen.verification ? `Checked ${new Date(gen.verification.checked_at * 1000).toLocaleString()}` : "Verification has not run for this installation."}</p>
      <details className={s.details}>
      <summary>Verification evidence · {gen.required_checks.filter(check => gen.verification?.results.some(r => r.check === check && r.passed)).length}/{gen.required_checks.length} checks passed</summary>
      <ul className={s.checks} aria-label="Verification evidence">{gen.required_checks.map(check => {
        const result = gen.verification?.results.find(r => r.check === check);
        return <li key={check}><span>{check}<small>{result?.detail ?? "Not run for this installation."}</small></span><b>{result ? result.passed ? "Passed" : "Failed" : "Pending"}</b></li>;
      })}</ul>
      </details>
    </section>)}
    <PluginActions key={id} id={id} label={label} data={data} onChange={onChange} />
  </section>;
}
