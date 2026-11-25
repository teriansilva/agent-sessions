import { useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { Link } from "react-router-dom";
import { settingsPath } from "./settingsTabs";
import { api } from "../lib/api";
import type { HqRouteSettings as Route } from "../types/api";
import styles from "./Settings.module.css";

const DEFAULTS: Route = { enabled: true, model: "", thinking: "off" };

/** Routing enablement stays here; model/thinking live with the other HQ model rows. */
export function HqRouteSettings() {
  const config = useConfig();
  const refresh = useConfigRefresh();
  // What the last save stored, shown until the config it asks for arrives (`HqReportsSettings`).
  const [override, setOverride] = useState<Route | null>(null);
  const [seenConfig, setSeenConfig] = useState(config);
  if (config !== seenConfig) {
    setSeenConfig(config);
    if (override !== null) setOverride(null);
  }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const route: Route = override ?? { ...DEFAULTS, ...(config?.hq_route ?? {}) };

  const save = async (patch: Partial<Route>) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.setPrefs({ hq_route: patch });
      const stored = (r as { hq_route?: Route }).hq_route;
      setOverride(stored ?? { ...route, ...patch });
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn’t save.");
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const disabled = !config || busy;
  return (
    <section className={styles.section} aria-labelledby="hq-route-h">
      <h2 id="hq-route-h">Question routing</h2>
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={route.enabled}
          disabled={disabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
          data-testid="hq-route-toggle"
        />
        <span>Search your sessions only when a question needs it</span>
      </label>
      <p className={styles.hint}>
        When your AI endpoint can&rsquo;t use tools, HQ first decides whether a
        message is about your sessions and missions. Questions like &ldquo;which
        model are you?&rdquo; are answered straight away; anything about your work
        still searches. Off: every message searches.
      </p>
      {err && (
        <p className={styles.err} role="alert">
          {err}
        </p>
      )}
      <p className={styles.hint}>
        Choose the deciding request&rsquo;s model and thinking under{ " " }
        <Link to={settingsPath("ai-hq", "hq-models-h")}>HQ models</Link>.
      </p>
    </section>
  );
}
