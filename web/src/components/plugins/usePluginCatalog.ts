import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { EngineInfo } from "../../types/api";
import type { PluginCatalog } from "../../types/plugins";
import { pluginError, verifiedGeneration } from "./model";

export function usePluginCatalog() {
  const [data, setData] = useState<PluginCatalog | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { setData(await api.plugins()); setError(""); } catch (e) { setError(pluginError(e)); }
  }, []);
  useEffect(() => {
    let alive = true;
    void api.plugins().then(value => { if (alive) setData(value); })
      .catch(e => { if (alive) setError(pluginError(e)); });
    return () => { alive = false; };
  }, []);
  const refresh = async () => {
    setBusy(true); setError("");
    try { setData(await api.pluginRefresh()); } catch (e) { await load(); setError(pluginError(e)); }
    finally { setBusy(false); }
  };
  const configure = async (automatic: boolean) => {
    setBusy(true); setError("");
    try { setData(await api.agentCatalogPreferences(automatic)); }
    catch (e) { setError(pluginError(e)); }
    finally { setBusy(false); }
  };
  return { data, error, busy, load, refresh, configure };
}
export type GalleryFilter = "All" | "Ready" | "Needs setup" | "Updates" | "Disabled";
export function galleryMatch(id: string, label: string, engine: EngineInfo | undefined, data: PluginCatalog | null, query: string, filter: GalleryFilter) {
  if (!`${id} ${label}`.toLowerCase().includes(query.trim().toLowerCase())) return false;
  const row = data?.plugins.find(p => p.id === id);
  const active = row?.generations?.find(g => g.id === row.active);
  const candidate = row?.generations?.find(g => g.id === row.candidate);
  const entry = data?.catalog.find(c => c.manifest.identity.id === id);
  if (filter === "Ready") return !!engine?.present && engine.status !== "retiring";
  if (filter === "Disabled") return !!row && row.enabled === false;
  if (filter === "Updates") return !!entry && !!active && entry.digest !== active.review.recipe_digest;
  if (filter === "Needs setup") return !engine?.present || !!row?.error || !!candidate && !verifiedGeneration(candidate);
  return true;
}
