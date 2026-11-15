import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { MEMORY_PATH } from "../lib/routes";
import type { MemorySearchSettings as Saved } from "../types/memory";
import styles from "./Settings.module.css";
import buttons from "../components/ui/actionButton.module.css";

/** Shared by HQ and all project notebooks; testing a draft leaves the active index in use. */
export function MemorySearchSettings() {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [tested, setTested] = useState(false);
  const [health, setHealth] = useState("Loading search status…");
  const generation = useRef(0);
  useEffect(() => {
    let live = true;
    api.memorySearchSettings().then(value => {
      if (live) { setSaved(value); setUrl(value.base_url); setModel(value.model); }
    }).catch(e => { if (live) setError(e instanceof Error ? e.message : "Connection could not be loaded."); });
    const refreshHealth = () => api.memorySearchStatus().then(value => {
      if (live) setHealth(value.purge_overdue ? "Search maintenance is overdue. Forgotten notes stay excluded." : value.state === "ready" ? "Keyword and semantic search ready" : value.state === "keyword-only" ? "Keyword search ready" : `Search ${value.state}`);
    }).catch(() => { if (live) setHealth("Search status unavailable"); });
    void refreshHealth();
    const timer = window.setInterval(() => { if (document.visibilityState !== "hidden") void refreshHealth(); }, 15_000);
    // Invalidate all request generations on unmount, including ones started after this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => { live = false; window.clearInterval(timer); generation.current++; };
  }, []);
  const changed = () => { generation.current++; setTested(false); setMessage(""); setError(""); };
  const body = () => ({ base_url: url, model, ...(clearKey ? { api_key: null } : key.trim() ? { api_key: key } : {}) });
  const run = async (action: "test" | "save") => {
    const id = ++generation.current;
    setBusy(true); setError(""); setMessage("");
    try {
      if (action === "test") {
        const value = await api.testMemorySearch(body());
        if (id === generation.current) { setMessage(value.detail); setTested(value.ok); }
      } else {
        const value = await api.setMemorySearchSettings(body());
        if (id === generation.current) {
          setSaved(value); setUrl(value.base_url); setModel(value.model); setKey(""); setClearKey(false);
          setMessage(value.configured && value.vectors?.available !== false ? "Saved. Keyword search stays available while semantic indexing catches up." : "Saved. Memory uses keyword search.");
        }
      }
    } catch (e) { if (id === generation.current) setError(e instanceof Error ? e.message : "That request failed."); }
    finally { if (id === generation.current) setBusy(false); }
  };
  return <section className={styles.section} aria-labelledby="memory-search-h" data-testid="memory-search-settings">
    <h2 id="memory-search-h">Memory search</h2>
    <p className={styles.hint}>HQ and project notebooks use one search system. Keyword search works immediately. Add your embeddings connection for semantic recall.</p>
    <p className={styles.hint} role="status">{health}</p>
    {saved?.vectors?.available === false && <p className={styles.hint}>Semantic search is unavailable on this host. Keyword search remains available.</p>}
    <div className={styles.aiField}>
      <label className={styles.aiFieldLabel} htmlFor="memory-search-url">Embeddings endpoint</label>
      <input id="memory-search-url" className={styles.aiInput} type="url" placeholder="https://your-endpoint/v1" value={url} disabled={!saved || busy} onChange={e => { changed(); setUrl(e.target.value); }} />
    </div>
    <div className={styles.aiField}>
      <label className={styles.aiFieldLabel} htmlFor="memory-search-model">Embedding model</label>
      <input id="memory-search-model" className={styles.aiInput} value={model} disabled={!saved || busy} onChange={e => { changed(); setModel(e.target.value); }} />
    </div>
    <div className={styles.aiField}>
      <label className={styles.aiFieldLabel} htmlFor="memory-search-key">API key (optional)</label>
      <input id="memory-search-key" className={styles.aiInput} type="password" autoComplete="new-password" value={key} placeholder={saved?.api_key_set ? "Saved · leave blank to keep" : "No key saved"} disabled={!saved || busy || clearKey} onChange={e => { changed(); setKey(e.target.value); }} />
    </div>
    {saved?.api_key_set && <label className={styles.aiToggle}><input type="checkbox" checked={clearKey} disabled={busy} onChange={e => { changed(); setClearKey(e.target.checked); }} /><span>Clear the saved key</span></label>}
    {saved?.needs_reentry && <p className={styles.err} role="alert">Re-enter or clear the saved key before using this connection.</p>}
    <p className={styles.hint}>Saving sends eligible note text and search queries to this endpoint. A changed origin needs a new key or an explicit clear. Testing uses a short sample and does not change the saved connection.</p>
    <div className={styles.aiActions}>
      <button type="button" className={buttons.ghost} disabled={!saved || busy || !url || !model || saved.vectors?.available === false} onClick={() => void run("test")}>{busy ? "Working…" : "Test draft connection"}</button>
      <button type="button" className={buttons.primary} disabled={!saved || busy} onClick={() => void run("save")}>Save connection</button>
    </div>
    {message && <p className={styles.hint} role="status">{message}</p>}
    {!tested && (url !== saved?.base_url || model !== saved?.model || !!key || clearKey) && <p className={styles.hint}>Draft changed · test again to verify these values.</p>}
    {error && <p className={styles.err} role="alert">{error}</p>}
    <p className={styles.hint}><Link to={`${MEMORY_PATH}?project=_hq`}>Manage HQ memory and optional capture</Link></p>
    <p className={styles.hint}>If your HQ endpoint cannot use tools, turn on Question routing above for memory recall and explicit remember requests. With routing off, use Library → Memory.</p>
  </section>;
}
