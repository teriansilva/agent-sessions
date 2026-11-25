import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { api, ApiError } from "../lib/api";
import { announceClustersChanged } from "../lib/clusterEvents";
import { relTime } from "../lib/format";
import type { ClustersConfig, ClustersList } from "../types/api";
import styles from "./Settings.module.css";
import { promptPath, settingsPath } from "./settingsTabs";

const FALLBACK: ClustersConfig = {
  enabled: true,
  agent_tool: true,
  interval_minutes: 30,
  configured: false,
};

/** The server's set (`prefs.CLUSTERS_INTERVALS`); anything else is refused there. */
const INTERVALS: { value: number; label: string }[] = [
  { value: 15, label: "Every 15 minutes" },
  { value: 30, label: "Every 30 minutes" },
  { value: 60, label: "Every hour" },
  { value: 240, label: "Every 4 hours" },
];

/** What the status line says about the last sweeps: the newest time, and the first error. */
function sweepSummary(list: ClustersList | null): { line: string; error: string | null } {
  if (!list) return { line: "", error: null };
  if (list.unreadable)
    return { line: "", error: "The cluster store cannot be read right now." };
  const sweeps = Object.values(list.sweeps).filter((s) => s != null);
  const last = sweeps.reduce((t, s) => Math.max(t, s.at ?? 0), 0);
  const projects = new Set(list.clusters.map((c) => c.project_id)).size;
  const n = list.clusters.length;
  const parts = [
    last ? `last sweep ${relTime(last)}` : "no sweep yet",
    `${projects} project${projects === 1 ? "" : "s"}`,
    `${n} cluster${n === 1 ? "" : "s"}`,
  ];
  return {
    line: parts.join(" · "),
    error: sweeps.find((s) => s.error)?.error ?? null,
  };
}

/** Settings → AI → Session clusters (#1997). HQ groups each project's sessions by topic with the
 *  AI endpoint from Endpoint & model. On by default; the stored clusters are kept while it is
 *  off. "Sessions see their cluster" is read when a session is launched. */
export function ClustersSettings() {
  const cfgBlock = useConfig()?.clusters;
  const refreshConfig = useConfigRefresh();
  const location = useLocation();
  const [block, setBlock] = useState<ClustersConfig>(cfgBlock ?? FALLBACK);
  const [synced, setSynced] = useState(cfgBlock);
  if (cfgBlock !== synced) {
    setSynced(cfgBlock);
    if (cfgBlock) setBlock(cfgBlock);
  }
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [list, setList] = useState<ClustersList | null>(null);
  const [listTick, setListTick] = useState(0);
  useEffect(() => {
    let alive = true;
    api
      .clusters()
      .then((r) => alive && setList(r))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [listTick]);

  const save = async (partial: Partial<ClustersConfig>) => {
    setError(null);
    try {
      const r = (await api.setPrefs({ clusters: partial })) as { clusters?: ClustersConfig };
      if (r.clusters) {
        setBlock(r.clusters);
        refreshConfig();
        // The sidebar's tree, chips and filter follow the switch at once.
        announceClustersChanged();
      }
    } catch {
      setError("Couldn’t save — please try again.");
    }
  };

  const runNow = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const rep = await api.recluster();
      const failed = rep.projects.filter((p) => p.status === "error");
      const regrouped = rep.projects.filter((p) => p.status === "committed").length;
      setResult(
        failed.length
          ? `HQ could not group ${failed.length} project${failed.length === 1 ? "" : "s"}: ${failed[0].error ?? "the AI endpoint failed"}.`
          : `HQ looked at ${rep.projects.length} project${rep.projects.length === 1 ? "" : "s"} and regrouped ${regrouped}.` +
              (rep.deferred ? ` ${rep.deferred} more will follow on the schedule.` : ""),
      );
      announceClustersChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Re-cluster failed — please try again.");
    } finally {
      setRunning(false);
      setListTick((t) => t + 1);
    }
  };

  const status = sweepSummary(list);
  const state = !block.enabled ? "Off" : block.configured ? "On" : "Waiting for an AI endpoint";

  return (
    <section className={styles.section} aria-labelledby="clusters-h">
      <h2 id="clusters-h">Session clusters</h2>
      <p className={styles.hint} data-testid="clusters-status">
        {[state, status.line].filter(Boolean).join(" · ")}
      </p>
      <p className={styles.hint}>
        HQ groups each project’s sessions by topic, using the same AI endpoint as session
        review. Titles you set are kept, and a session you move stays where you put it.
      </p>
      {error && (
        <p className={styles.err} role="alert">
          {error}
        </p>
      )}
      {status.error && (
        <p className={styles.err} data-testid="clusters-sweep-error">
          The last sweep failed: {status.error}
        </p>
      )}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.enabled}
          onChange={(e) => void save({ enabled: e.currentTarget.checked })}
        />
        <span>Group sessions into clusters</span>
      </label>
      <p className={styles.hint}>
        Shows the clusters in the sidebar’s project tree and the cluster on each session
        row. Off hides them and stops the sweep; your clusters come back when you turn it on
        again.
      </p>
      {block.enabled && !block.configured && (
        <p className={styles.hint}>
          Clusters need an AI endpoint. Set one in{" "}
          <Link className={styles.nameLink} to={settingsPath("ai-endpoint")} state={location.state}>
            Endpoint &amp; model
          </Link>
          .
        </p>
      )}

      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={block.agent_tool}
          disabled={!block.enabled}
          onChange={(e) => void save({ agent_tool: e.currentTarget.checked })}
        />
        <span>Sessions see their cluster</span>
      </label>
      <p className={styles.hint}>
        Every agent gets the BattleLab cluster tool: its cluster or mission, what the other
        sessions there are doing, and a shared board. Applies from each session’s next launch.
      </p>

      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor="clusters-interval">
          Re-cluster
        </label>
        <div className={styles.aiIntervalRow}>
          <select
            id="clusters-interval"
            className={styles.aiInput}
            value={block.interval_minutes}
            disabled={!block.enabled}
            onChange={(e) => void save({ interval_minutes: Number(e.currentTarget.value) })}
          >
            {INTERVALS.map((i) => (
              <option key={i.value} value={i.value}>
                {i.label}
              </option>
            ))}
          </select>
        </div>
        <p className={styles.hint}>
          How often HQ looks again. Only projects whose sessions changed are sent.
        </p>
      </div>

      <div className={styles.aiField}>
        <p className={styles.hint}>
          The grouping instruction lives in{" "}
          <Link className={styles.nameLink} to={promptPath("session_clusters")} state={location.state}>
            Prompts → Topic clusters
          </Link>
          .
        </p>
      </div>

      <div className={styles.aiActions}>
        <button
          type="button"
          className={`${styles.secBtn} shine`}
          disabled={!block.enabled || !block.configured || running}
          onClick={() => void runNow()}
        >
          {running ? "Re-clustering…" : "Re-cluster now"}
        </button>
      </div>
      <p className={styles.hint} role="status">
        {result}
      </p>
    </section>
  );
}
