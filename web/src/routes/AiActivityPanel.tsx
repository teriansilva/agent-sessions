import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { AiActivity } from "../types/api";
import styles from "./Settings.module.css";

// The AI task kinds the platform registers (#441). Listed in a fixed order so the panel is
// stable; an unknown kind that turns up running is appended so nothing is hidden.
const KINDS: { kind: string; label: string }[] = [
  { kind: "pulse-scan", label: "Pulse scan" },
  { kind: "ai-review", label: "AI review" },
  { kind: "auto-sort", label: "Auto-sort" },
];

const POLL_MS = 3000;

function elapsed(since: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - since));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

function ago(at: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - at));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Shared AI-activity panel (#441 Phase 6): what AI work is running right now (Pulse scans,
 *  AI-review / auto-sort sweeps + their on-demand runs) plus the last run per kind. Polls
 *  /api/ai/activity while mounted — the first home for the platform's growing set of AI
 *  features, and how the "two scans never overlap" guarantee becomes observable. */
export function AiActivityPanel() {
  const [activity, setActivity] = useState<AiActivity | null>(null);
  // A 1s ticker so a running task's elapsed time advances between the slower polls.
  const [, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    const poll = () =>
      api
        .aiActivity()
        .then((a) => live && setActivity(a))
        .catch(() => {});
    void poll();
    const pid = setInterval(() => void poll(), POLL_MS);
    const tid = setInterval(() => live && setTick((t) => t + 1), 1000);
    return () => {
      live = false;
      clearInterval(pid);
      clearInterval(tid);
    };
  }, []);

  const running = activity?.running ?? [];
  const last = activity?.last ?? {};
  // Known kinds first, then any running kind we didn't anticipate.
  const extraRunning = running
    .map((r) => r.kind)
    .filter((k) => !KINDS.some((x) => x.kind === k));
  const rows = [
    ...KINDS,
    ...[...new Set(extraRunning)].map((k) => ({ kind: k, label: k })),
  ];

  return (
    <section className={styles.section} aria-labelledby="ai-activity-h">
      <h2 id="ai-activity-h">AI activity</h2>
      <p className={styles.hint}>
        What AI work is running right now across the platform, plus the last run
        of each. Scans of the same kind never overlap — a second one waits for
        the first.
      </p>
      <ul className={styles.activityList} aria-live="polite">
        {rows.map(({ kind, label }) => {
          const run = running.find((r) => r.kind === kind);
          const seen = last[kind];
          return (
            <li key={kind} className={styles.activityRow}>
              <span
                className={`hud-led ${run ? "up" : "idle"}`}
                aria-hidden="true"
              />
              <span className={styles.activityName}>{label}</span>
              <span className={styles.activityState}>
                {run
                  ? `running ${elapsed(run.started_at)}${run.detail ? ` · ${run.detail}` : ""}`
                  : seen
                    ? `${seen.ok ? "ran" : "failed"} ${ago(seen.finished_at)}`
                    : "idle"}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
