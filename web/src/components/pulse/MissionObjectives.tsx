/** The objective list, with its probe states (#878).
 *
 * The rule that shapes this component: **nothing is marked met or failed on data the server
 * could not fetch.** When a probe could not run, the row renders its LAST OBSERVED state with
 * the time it was observed, visibly stale, and names the reason. A degraded probe that silently
 * reports the previous answer as current is the same lie as a stale 200, one layer up.
 */
import type { MissionObjective } from "../../types/api";

import styles from "./mission.module.css";

function when(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Semantic state colour. `met` is the only "good" state; everything else is neutral or amber. */
function dotFor(o: MissionObjective): string {
  if (o.state === "met") return styles.dotRunning;
  if (o.state === "failed") return styles.dotFailed;
  if (o.state === "waived") return styles.dotDone;
  return styles.dot;
}

/** What the probe last actually saw, if it is not current. `observed` is a free-form record from
 *  the server; only the two fields this surface promises are read, and each is checked rather
 *  than assumed, so a shape change degrades to "no staleness shown" instead of throwing. */
function staleness(o: MissionObjective): { seen: string; reason: string } | null {
  const obs = o.observed;
  if (!obs || typeof obs !== "object") return null;
  const stale = (obs as Record<string, unknown>).stale;
  if (stale !== true) return null;
  const at = (obs as Record<string, unknown>).at;
  const reason = (obs as Record<string, unknown>).reason;
  return {
    seen: typeof at === "number" ? when(at) : "",
    reason: typeof reason === "string" ? reason : "",
  };
}

export function MissionObjectives({
  objectives,
}: {
  objectives: MissionObjective[];
}) {
  if (objectives.length === 0) {
    return (
      <div className={styles.empty} data-testid="objectives-empty">
        No objectives yet. They define what done means for this mission.
      </div>
    );
  }
  return (
    <ul
      style={{ listStyle: "none", margin: 0, padding: 0 }}
      aria-label="Objectives"
      data-testid="objectives"
    >
      {objectives.map((o) => {
        const st = staleness(o);
        return (
          <li key={o.key} className={styles.objRow} data-testid="objective">
            <span className={`${styles.dot} ${dotFor(o)}`} aria-hidden="true" />
            <span style={{ minWidth: 0 }}>
              <span className={styles.objTitle}>{o.title}</span>
              {/* The state is announced in text too — the dot alone is not readable
                  by a screen reader, and status colour is load-bearing here. */}
              <span className={styles.objWhen}>
                {o.state}
                {o.met_at ? ` ${when(o.met_at)}` : ""}
              </span>
              {st ? (
                <>
                  <span className={styles.objStale} data-testid="objective-stale">
                    last seen {st.seen || "earlier"} · stale
                  </span>
                  {st.reason ? (
                    <span className={styles.objReason}>{st.reason}</span>
                  ) : null}
                </>
              ) : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
