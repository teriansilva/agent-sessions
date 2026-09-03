/** The event log (#878).
 *
 * Pagination is by CURSOR (`events_next_seq`), never by page offset. That is not a performance
 * choice: events arrive while the operator reads, and an offset window shifts under them, so a
 * second page would duplicate rows or skip them. A cursor names a position in the log itself.
 *
 * **A relay's OUTCOME is rendered, not just its text** (#903 review 3, finding 5). The route
 * stores what happened to the operator's words in the event's own `meta`, and a row that showed
 * only the text made a refused, ambiguous or still-in-flight relay look exactly like a delivered
 * one — the words on screen with nothing to say they never arrived. After a reload that is the
 * only record there is, so this is where the outcome has to appear.
 *
 * The four states are deliberately four different things and not two:
 *
 * * `delivered` — it landed. Stated, because "no news" is not the same as "yes";
 * * `sending` — in flight RIGHT NOW, or a record whose fate nobody can establish yet. Says so
 *   rather than implying either end;
 * * `indeterminate` — it may or may not have landed, and the operator is the one who has to
 *   decide whether to send it again. Never dressed up as a failure, which would invite a second
 *   copy of an instruction the agent may already have;
 * * anything else (`failed`, `expired`, `rejected`) — a definite non-delivery, with the server's
 *   own detail beside it.
 */
import type { MissionEvent } from "../../types/api";

import styles from "./mission.module.css";

/** The relay outcome for one event, or null when the event is not a relay.
 *
 *  Read from `meta`, which is where the route settles it, so a reload renders the same answer the
 *  live send did. The wording is the operator's, not the state name: `indeterminate` is a word
 *  about a database row, and what the operator needs to know is that they have a decision. */
function relayOutcome(e: MissionEvent): { label: string; kind: string } | null {
  const meta = e.meta;
  if (
    !meta ||
    typeof meta !== "object" ||
    !(meta as Record<string, unknown>).relay
  )
    return null;
  const m = meta as Record<string, unknown>;
  const state = typeof m.state === "string" ? m.state : "";
  const detail = typeof m.detail === "string" ? m.detail : "";
  if (state === "delivered") return { label: "delivered", kind: "ok" };
  if (state === "sending") return { label: "sending…", kind: "wait" };
  if (state === "indeterminate") {
    return {
      label: detail
        ? `may not have arrived — ${detail}`
        : "may not have arrived; check the session before sending it again",
      kind: "warn",
    };
  }
  if (!state) return null;
  return { label: detail ? `${state} — ${detail}` : state, kind: "bad" };
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function MissionTimeline({
  events,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  events: MissionEvent[];
  hasMore: boolean;
  loadingMore?: boolean;
  onLoadMore: () => void;
}) {
  if (events.length === 0) {
    return (
      <div className={styles.empty} data-testid="timeline-empty">
        Nothing has happened yet.
      </div>
    );
  }
  return (
    <div data-testid="timeline">
      <ol
        style={{ listStyle: "none", margin: 0, padding: 0 }}
        aria-label="Timeline"
      >
        {events.map((e) => (
          <li key={e.seq} className={styles.tlRow} data-testid="timeline-row">
            <span className={styles.tlAt}>{when(e.at)}</span>
            <span className={styles.tlKind}>{e.kind}</span>
            {/* Model-derived text, rendered as TEXT. React escapes it; there is no
                `dangerouslySetInnerHTML` anywhere in these components. */}
            <span className={styles.tlText}>{e.text ?? ""}</span>
            {(() => {
              const out = relayOutcome(e);
              return out ? (
                <span
                  className={styles.tlRelay}
                  data-testid="timeline-relay-state"
                  data-relay-kind={out.kind}
                >
                  {out.label}
                </span>
              ) : null;
            })()}
          </li>
        ))}
      </ol>
      {hasMore ? (
        <button
          type="button"
          className={styles.more}
          onClick={onLoadMore}
          disabled={loadingMore}
          data-testid="timeline-more"
        >
          {loadingMore ? "Loading…" : "Load older"}
        </button>
      ) : null}
    </div>
  );
}
