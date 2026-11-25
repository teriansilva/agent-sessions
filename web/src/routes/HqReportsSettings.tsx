import { useState } from "react";
import { useConfig, useConfigRefresh } from "../app/config";
import { api } from "../lib/api";
import styles from "./Settings.module.css";

/** Settings → HQ (#1882): whether sessions BattleLab starts can report to HQ. Off by default (#1882) until #1878 P1b. It
 *  adds the `battlelab_hq` tool to NEW terminal launches whose agent loads BattleLab's tools; a
 *  running agent keeps what it was started with. The tool can't act: a report the Commander
 *  needs to answer shows in Needs you, announced like any other. */
export function HqReportsSettings() {
  const config = useConfig();
  const refresh = useConfigRefresh();
  // What the last save stored, shown until the config it asks for arrives (`AnalyticsCard`'s rule).
  const [override, setOverride] = useState<boolean | null>(null);
  const [seenConfig, setSeenConfig] = useState(config);
  if (config !== seenConfig) {
    setSeenConfig(config);
    if (override !== null) setOverride(null);
  }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const on = override ?? config?.hq_reports ?? false;

  const save = async (value: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.setPrefs({ hq_reports: value });
      setOverride(typeof r.hq_reports === "boolean" ? r.hq_reports : value);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn’t save.");
    } finally {
      setBusy(false);
      refresh();
    }
  };

  return (
    <section className={styles.section} aria-labelledby="hq-reports-h">
      <h2 id="hq-reports-h">Session reports</h2>
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={on}
          disabled={!config || busy}
          onChange={(e) => void save(e.currentTarget.checked)}
          data-testid="hq-reports-toggle"
        />
        <span>Sessions can report to HQ</span>
      </label>
      {err && (
        <p className={styles.err} role="alert">
          {err}
        </p>
      )}
      <p className={styles.hint}>
        Agents BattleLab starts in a terminal get a tool to tell you they are
        blocked, need you to decide, are making progress, or are done. A blocked
        agent or a question shows in Needs you, in the agent&rsquo;s own words;
        progress shows on the session&rsquo;s row. The tool can&rsquo;t act on
        anything: only your tap answers a question.
      </p>
      <p className={styles.hint}>
        Applies to sessions started after you change it. Running sessions keep
        the tool they were started with.
      </p>
    </section>
  );
}
