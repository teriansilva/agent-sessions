import { useEffect, useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api } from "../lib/api";
import type { HqRouteSettings as Route, HqRouteThinking } from "../types/api";
import styles from "./Settings.module.css";

const DEFAULTS: Route = { enabled: true, model: "", thinking: "off" };

const THINKING: { value: HqRouteThinking; label: string }[] = [
  { value: "off", label: "Off · fastest" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "hq", label: "Same as HQ" },
];

/** Settings → AI → HQ → Question routing (#2028). When HQ's AI endpoint takes no tools, one small
 *  call decides whether a message needs a search of the sessions and missions, or can be answered
 *  directly. Off: every message searches, as before. Its model and thinking apply to that one call
 *  only; an empty model is HQ's own (on a call, the call's). Saved on change, like the rest of
 *  this page. */
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
  // The endpoint's own model list. One that can't be read leaves "Same as HQ" and whatever is
  // saved: never an invented id (`HqCallsSettings`' rule).
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    api
      .aiReviewModels()
      .then((r) => {
        const list = Array.isArray(r?.models) ? r.models.filter((m) => typeof m === "string" && m) : [];
        if (live) setModels(list);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
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
      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="hq-route-model">
          Model for this decision
        </label>
        <select
          id="hq-route-model"
          className={styles.aiInput}
          value={route.model}
          disabled={disabled || !route.enabled}
          onChange={(e) => void save({ model: e.currentTarget.value })}
          data-testid="hq-route-model"
        >
          <option value="">Same as HQ</option>
          {/* The saved model stays choosable even when the endpoint no longer lists it. */}
          {[...new Set([...(route.model ? [route.model] : []), ...models])].map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="hq-route-thinking">
          Thinking for this decision
        </label>
        <select
          id="hq-route-thinking"
          className={styles.aiInput}
          value={route.thinking}
          disabled={disabled || !route.enabled}
          onChange={(e) => void save({ thinking: e.currentTarget.value as HqRouteThinking })}
          data-testid="hq-route-thinking"
        >
          {THINKING.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {err && (
        <p className={styles.err} role="alert">
          {err}
        </p>
      )}
      <p className={styles.hint}>
        These apply to the deciding call only. HQ&rsquo;s answers keep the model
        and thinking set under Endpoint &amp; model (or HQ calls, on a call).
      </p>
    </section>
  );
}
