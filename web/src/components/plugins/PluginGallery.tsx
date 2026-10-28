/** Installer surfaces of Settings → Agents (#853, #1408): the agent page's ⋯ menu (disable /
 *  remove / reload), its one-line installation summary, and the roster's one-line catalog strip.
 *
 *  #1408 moved WHERE these render, not what they do: the disable/remove request-id flow below —
 *  persist before POST, recover a previous attempt, settle against the operation record — is the
 *  one `PluginActions` had, unchanged, and the confirmation copy is the same. */
import { Ellipsis } from "lucide-react";
import { useState, type MouseEvent, type ReactNode } from "react";
import { reloadRoster } from "../../app/reloadRoster";
import { api } from "../../lib/api";
import type { PluginCatalog, PluginGeneration } from "../../types/plugins";
import { AgentCatalogStatus } from "../agents/AgentCatalogStatus";
import r from "../agents/Roster.module.css";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { AnchoredMenu } from "../ui/AnchoredMenu";
import button from "../ui/actionButton.module.css";
import { missingOperation, pluginError, rejectedOperation, sourceLabel, verifiedGeneration } from "./model";
import type { usePluginCatalog } from "./usePluginCatalog";

type Catalog = ReturnType<typeof usePluginCatalog>;
type Kind = "disable" | "remove";
type Attempt = { request_id: string; plugin_id: string; kind: Kind; expected_active?: string | null; expected_revision?: string | null };

/** Disable / remove with a durable request id (unchanged from `PluginActions`). */
function useDeactivate(id: string, label: string, data: PluginCatalog | null, onChange: () => Promise<void>) {
  const row = data?.plugins.find(p => p.id === id);
  const [confirm, setConfirm] = useState<{ kind: Kind; trigger: HTMLElement; active: string | null; revision: string | null } | null>(null);
  const attemptKey = `plugin-deactivate:${id}`;
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
  const open = (kind: Kind, trigger: HTMLElement) =>
    setConfirm({ kind: attempt?.kind ?? kind, trigger, active: row?.active ?? null, revision: data?.roster_revision ?? null });
  const dialog = confirm && <ConfirmDialog tag="AGENT INSTALLATION" title={`${confirm.kind === "remove" ? "Remove" : "Disable"} ${label}?`}
    confirmLabel={confirm.kind === "remove" ? "Remove agent" : "Disable agent"} busy={busy}
    returnFocusTo={confirm.trigger} onCancel={() => { setConfirm(null); setError(""); }} onConfirm={() => void deactivate()}>
    <p>New sessions will be unavailable. Running sessions stay attachable until they exit.</p>
    <p>Vendor transcripts, credentials and installed copies stay on this host. Saved defaults and budgets are retained.</p>
    {error && <p role="alert">{error}</p>}
  </ConfirmDialog>;
  return { row, attempt, open, dialog, error: confirm ? "" : error };
}

/** The agent page's ⋯ (#1408): everything rare or destructive, behind one trigger. */
export function AgentMenu({ id, label, data, onChange }: {
  id: string; label: string; data: PluginCatalog | null; onChange: () => Promise<void>;
}) {
  const { row, attempt, open, dialog, error } = useDeactivate(id, label, data, onChange);
  const [reloading, setReloading] = useState(false);
  const [reloadError, setReloadError] = useState("");
  const reload = async () => {
    setReloading(true); setReloadError("");
    try { await api.pluginReload(); await reloadRoster(); await onChange(); }
    catch (e) { setReloadError(pluginError(e)); }
    finally { setReloading(false); }
  };
  return <>
    <AnchoredMenu
      label={`More actions for ${label}`}
      trigger={<Ellipsis size={18} aria-hidden="true" />}
      triggerClassName={button.icon}
      triggerTestId="agent-menu"
      menuTestId="agent-menu-items"
      focus="first-item"
      // Portalled (#1574): the agent hero and the roster sit in NIGHTJAR panels, and a panel clips
      // (`nj-frame`'s chamfer clip-path) — the in-tree panel drew but its rows outside the frame
      // could not be clicked (plugin-setup "Disable…" landed on the detail underneath).
      portal
      classes={{ wrap: r.menuWrap, panel: r.menuPanel, items: r.menuItems }}
    >
      {(close, menuTrigger) => {
        // The dialog returns focus to the ⋯ TRIGGER: the item that opened it is gone once the menu
        // closes, and focus returned to a removed node is focus lost. AnchoredMenu hands the trigger
        // over, because on a phone the menu is a portalled sheet with no DOM path back to it.
        const act = (fn: (trigger: HTMLElement) => void) => (e: MouseEvent<HTMLButtonElement>) => {
          const trigger = menuTrigger() ?? e.currentTarget;
          close();
          fn(trigger);
        };
        return <>
          <button type="button" role="menuitem" className={r.menuItem} disabled={reloading} onClick={act(() => void reload())}>
            {reloading ? "Reloading…" : "Reload agent roster"}
          </button>
          {attempt && <button type="button" role="menuitem" className={r.menuItem} onClick={act(t => open(attempt.kind, t))}>
            Check previous {attempt.kind}
          </button>}
          {row?.enabled && <button type="button" role="menuitem" className={`${r.menuItem} ${r.menuDanger}`} onClick={act(t => open("disable", t))}>
            Disable…
          </button>}
          {row && <button type="button" role="menuitem" className={`${r.menuItem} ${r.menuDanger}`} onClick={act(t => open("remove", t))}>
            Remove…
          </button>}
        </>;
      }}
    </AnchoredMenu>
    {(error || reloadError) && <p role="alert" className={r.inlineError}>{error || reloadError}</p>}
    {dialog}
  </>;
}

function checksLine(gen: PluginGeneration): string {
  const passed = gen.required_checks.filter(check => gen.verification?.results.some(x => x.check === check && x.passed)).length;
  return `${passed}/${gen.required_checks.length} checks passed`;
}

/** Installation as ONE summary line plus an evidence disclosure (#1408). */
export function InstallationSummary({ id, data, error, fallback }: {
  id: string; data: PluginCatalog | null; error?: string;
  /** What to say when the installer manages nothing for this agent. */
  fallback: ReactNode;
}) {
  const row = data?.plugins.find(p => p.id === id);
  const generations = row?.generations?.filter(g => g.id === row.active || g.id === row.candidate) ?? [];
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p role="status" className={r.muted}>Loading installation…</p>;
  if (!row) return <p className={r.muted} data-testid="installation-summary">{fallback}</p>;
  return <div className={r.install}>
    {row.error && <p role="alert">{row.error}</p>}
    {generations.map(gen => {
      const isActive = gen.id === row.active;
      const ok = verifiedGeneration(gen);
      return <div key={gen.id} aria-label={isActive ? "Active installation" : "Candidate installation"}>
        <p className={r.summary} data-testid="installation-summary">
          <b>{isActive ? "Active" : "Candidate"} · {gen.review.entry.manifest.identity.version}</b>
          <span>·</span><span>{sourceLabel(gen.review)}</span>
          <span>·</span><span>{isActive && row.enabled ? "Enabled" : "Disabled"}</span>
          <span>·</span>
          <span className={r.state}><span className={`hud-led ${ok ? "up" : "attention"}`} aria-hidden="true" />{checksLine(gen)}</span>
          {gen.verification && <><span>·</span><span>checked {new Date(gen.verification.checked_at * 1000).toLocaleString()}</span></>}
        </p>
        <details className={r.disclosure}>
          <summary>Verification evidence</summary>
          <ul className={r.checks} aria-label="Verification evidence">{gen.required_checks.map(check => {
            const result = gen.verification?.results.find(x => x.check === check);
            return <li key={check}><span>{check}<small>{result?.detail ?? "Not run for this installation."}</small></span><b>{result ? result.passed ? "Passed" : "Failed" : "Pending"}</b></li>;
          })}</ul>
        </details>
      </div>;
    })}
  </div>;
}

const rel = (seconds: number | null | undefined) => {
  if (!seconds) return "not checked yet";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
  return mins < 1 ? "checked just now" : mins < 60 ? `checked ${mins} min ago` : mins < 48 * 60 ? `checked ${Math.round(mins / 60)} h ago` : `checked ${Math.round(mins / 1440)} d ago`;
};

/** The catalog as ONE line under the roster (#1408); the full panel is behind Details. */
export function CatalogLine({ catalog }: { catalog: Catalog }) {
  const { data, error, busy, load, refresh, configure, configureAlerts } = catalog;
  const feed = data?.feed;
  const failed = !!(feed?.refresh?.error || feed?.error);
  // The catalog's own diagnostic (e.g. unavailable trust records) is shown, never just "failed".
  const why = feed?.error ?? null;
  const source = feed?.state === "unavailable" ? "Trust records unavailable"
    : feed?.source === "bundled" ? `Bundled with ${feed.release_version ?? "this release"}`
      : feed?.sequence ? `Signed catalog · sequence ${feed.sequence}` : "Loading catalog…";
  return <div className={r.catalogLine} data-testid="catalog-line">
    {error && <p className={r.lineAlert} role="alert">{error}<button type="button" className={button.ghost} onClick={() => void load()}>Retry loading</button></p>}
    <div className={r.lineRow}>
      <span className={r.lineLabel}>Catalog</span>
      {why
        ? <span className={r.state} role="alert" data-testid="catalog-error"><span className="hud-led attention" aria-hidden="true" />{why}</span>
        : failed
        ? <span className={r.state} role="status"><span className="hud-led attention" aria-hidden="true" />Last check failed — previous catalog kept</span>
        : <span>{source} · {rel(feed?.refresh?.last_success)}{feed?.refresh?.automatic ? " · checks daily" : ""}</span>}
      <button type="button" className={button.ghost} disabled={busy} onClick={() => void refresh()}>
        {busy ? "Refreshing…" : failed ? "Retry" : "Refresh"}
      </button>
    </div>
    <details className={r.disclosure}>
      <summary>Catalog details</summary>
      <AgentCatalogStatus feed={feed} busy={busy} refresh={() => void refresh()} configure={value => void configure(value)} configureAlerts={value => void configureAlerts(value)} />
    </details>
  </div>;
}
