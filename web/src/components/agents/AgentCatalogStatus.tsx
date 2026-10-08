import type { PluginCatalog } from "../../types/plugins";
import button from "../ui/actionButton.module.css";
import s from "../plugins/PluginSetup.module.css";

const date = (value: number | null | undefined) => value ? new Date(value * 1000).toLocaleString() : "Not checked yet";

export function AgentCatalogStatus({ feed, busy, refresh, configure }: {
  feed: PluginCatalog["feed"] | undefined; busy: boolean;
  refresh: () => void; configure: (automatic: boolean) => void;
}) {
  return <section className={`${s.panel} ${s.stack}`} aria-label="Public agent catalog">
    <div className={s.row}>
      <h2>Public agent catalog</h2>
      <span>{feed?.state === "unavailable" ? "Trust records unavailable" : feed?.source === "bundled" ? "Bundled with this release" : feed?.sequence ? `Signed catalog · sequence ${feed.sequence}` : "Loading catalog…"}</span>
    </div>
    <p>Agent definitions ship with BattleLab. Signed updates are checked on public GitHub.</p>
    {feed?.stale && <p className={s.note}>The accepted remote catalog has expired. Only unchanged recipes also bundled with this release remain installable.</p>}
    {feed?.refresh?.error && <p className={s.note} role="status">{feed.refresh.error}</p>}
    {feed?.refresh && <dl className={s.kv}>
      <dt>Last attempt</dt><dd>{date(feed.refresh.last_attempt)}</dd>
      <dt>Last successful check</dt><dd>{date(feed.refresh.last_success)}</dd>
    </dl>}
    <div className={s.row}>
      {feed?.definitions_url && <a className={button.ghost} href={feed.definitions_url} target="_blank" rel="noreferrer">View definitions</a>}
      {feed?.history_url && <a className={button.ghost} href={feed.history_url} target="_blank" rel="noreferrer">Change history</a>}
      {feed?.updates_url && <a className={button.ghost} href={feed.updates_url} target="_blank" rel="noreferrer">GitHub releases</a>}
    </div>
    <div className={s.row}>
      {feed?.refresh && <label className={s.check}><input type="checkbox" checked={feed.refresh.automatic} disabled={busy} onChange={event => configure(event.target.checked)} />Automatically check daily</label>}
      <button className={button.ghost} disabled={busy} onClick={refresh}>{busy ? "Refreshing…" : "Refresh catalog"}</button>
    </div>
    <p>Installing an agent update is a separate action.</p>
    {feed?.digest && <details className={s.details}><summary>Catalog evidence</summary>
      <p>Installed BattleLab release: {feed.release_version ?? "Unknown"}</p>
      <p className={s.digest}>Catalog SHA-256: {feed.digest}</p>
      {feed.bundled_digest && <p className={s.digest}>Bundled SHA-256: {feed.bundled_digest}</p>}
      {feed.expires_at && <p>Remote validity ends: {date(feed.expires_at)}</p>}
    </details>}
  </section>;
}
