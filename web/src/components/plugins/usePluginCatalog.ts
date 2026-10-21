import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { PluginCatalog } from "../../types/plugins";
import { pluginError } from "./model";

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
