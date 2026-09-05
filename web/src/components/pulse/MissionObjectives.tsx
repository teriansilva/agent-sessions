/** The objective list, with its probe states (#878) and the operator's edits (#889).
 *
 * The rule that shapes this component: **nothing is marked met or failed on data the server
 * could not fetch.** When a probe could not run, the row renders its LAST OBSERVED state with
 * the time it was observed, visibly stale, and names the reason. A degraded probe that silently
 * reports the previous answer as current is the same lie as a stale 200, one layer up.
 *
 * **An edit is never a claim that an objective holds (#889).** `PATCH /objectives` refuses
 * `state` / `met_at` / `observed` at the route, so the only settlement an operator can write is
 * `waived` — a decision that the objective was not *required*, which is a different claim from
 * having observed it hold. The control is labelled for that meaning ("NOT REQUIRED"), never
 * "met", and the supervisor's completion proposal repeats the distinction when it quotes the
 * list back.
 *
 * **An empty list is not the same as no objectives.** `POST /api/missions` returns before the
 * producer has run, so a mission legitimately has nothing here for a moment. `objectives_state`
 * says which case this is, and the empty state renders it — "working out what done means" while
 * pending, the named reason on `failed` / `skipped`. Rendering an unanswered question as an
 * answer is the same family of lie as the stale probe above.
 */
import { useCallback, useState } from "react";

import type { MissionObjective } from "../../types/api";

import styles from "./mission.module.css";

/** One `PATCH /objectives` op. The shapes the route accepts; `state` is not among them. */
export type ObjectiveOp =
  | { op: "add"; key: string; title: string; gate?: boolean }
  | { op: "drop"; key: string }
  | { op: "retitle"; key: string; title: string }
  | { op: "waive"; key: string }
  | { op: "reorder"; keys: string[] };

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
function staleness(
  o: MissionObjective,
): { seen: string; reason: string } | null {
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

/** The empty state, which has four different meanings and must not flatten them into one. */
function EmptyObjectives({
  objectivesState,
}: {
  objectivesState?: string | null;
}) {
  if (objectivesState === "pending") {
    return (
      <div className={styles.empty} data-testid="objectives-pending">
        Working out what done means for this mission…
      </div>
    );
  }
  if (objectivesState === "failed" || objectivesState === "skipped") {
    return (
      <div className={styles.empty} data-testid="objectives-unavailable">
        <div className={styles.emptyLead}>
          {objectivesState === "skipped"
            ? "No objectives were proposed."
            : "The objective list could not be produced."}
        </div>
        <div>The timeline says why. You can still add them by hand.</div>
      </div>
    );
  }
  return (
    <div className={styles.empty} data-testid="objectives-empty">
      No objectives yet. They define what done means for this mission.
    </div>
  );
}

export function MissionObjectives({
  objectives,
  objectivesState,
  onOps,
}: {
  objectives: MissionObjective[];
  /** `pending` / `done` / `failed` / `skipped` from the mission row — see the module note. */
  objectivesState?: string | null;
  /** Apply operator edits. Absent ⇒ the list is read-only (an archived or closed mission).
   *  Ops are handed over as an ARRAY and posted as one batch, because the route applies them in
   *  a single transaction — a reorder that half-applied would leave an order nobody chose. */
  /** Returns whether the SERVER accepted the ops. A caller holding the operator's typing needs
   *  that answer: clearing a draft the server rejected turns the retry into a retype. */
  onOps?: (ops: ObjectiveOp[]) => Promise<boolean>;
}) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  /** Which row is being retitled, and its draft. One at a time: a list where several rows are
   *  simultaneously mid-rename is a list whose order is hard to reason about while it moves. */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [rename, setRename] = useState("");

  /** The full key list with the row at `i` moved by `delta`. The route replaces the whole order,
   *  so the move is computed here and sent once rather than as a sequence the store would have to
   *  reconcile. */
  const moved = useCallback(
    (i: number, delta: number): string[] => {
      const keys = objectives.map((o) => o.key);
      const j = i + delta;
      if (j < 0 || j >= keys.length) return keys;
      const out = [...keys];
      [out[i], out[j]] = [out[j], out[i]];
      return out;
    },
    [objectives],
  );

  const run = useCallback(
    async (key: string, ops: ObjectiveOp[]): Promise<boolean> => {
      if (!onOps || busyKey) return false;
      setBusyKey(key);
      try {
        return await onOps(ops);
      } finally {
        setBusyKey(null);
      }
    },
    [onOps, busyKey],
  );

  const add = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      const title = adding.trim();
      if (!title || !onOps || busyKey) return;
      // A client-minted key. The route validates the shape; uniqueness is the store's job and a
      // collision comes back as a refusal rather than overwriting an existing objective.
      const key = `op-${crypto.randomUUID().slice(0, 8)}`;
      // KEPT ON A REFUSAL. A duplicate key, a mission that has become read-only, a 409 from a
      // concurrent edit — every one of those is something the operator can act on, and every one
      // of them used to cost them their typing.
      if (await run(key, [{ op: "add", key, title }])) setAdding("");
    },
    [adding, onOps, busyKey, run],
  );

  if (objectives.length === 0 && !onOps) {
    return <EmptyObjectives objectivesState={objectivesState} />;
  }

  return (
    <>
      {objectives.length === 0 ? (
        <EmptyObjectives objectivesState={objectivesState} />
      ) : (
        <ul
          style={{ listStyle: "none", margin: 0, padding: 0 }}
          aria-label="Objectives"
          data-testid="objectives"
        >
          {objectives.map((o, i) => {
            const st = staleness(o);
            const settled = o.state === "met" || o.state === "waived";
            return (
              <li key={o.key} className={styles.objRow} data-testid="objective">
                <span
                  className={`${styles.dot} ${dotFor(o)}`}
                  aria-hidden="true"
                />
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
                      <span
                        className={styles.objStale}
                        data-testid="objective-stale"
                      >
                        last seen {st.seen || "earlier"} · stale
                      </span>
                      {st.reason ? (
                        <span className={styles.objReason}>{st.reason}</span>
                      ) : null}
                    </>
                  ) : null}
                  {onOps && renaming === o.key ? (
                    <form
                      className={styles.objAddRow}
                      onSubmit={(e) => {
                        e.preventDefault();
                        const title = rename.trim();
                        if (!title) return;
                        void run(o.key, [
                          { op: "retitle", key: o.key, title },
                        ]).then((ok) => ok && setRenaming(null));
                      }}
                      aria-label={`Rename "${o.title ?? o.key}"`}
                    >
                      <input
                        className={styles.objAddInput}
                        value={rename}
                        onChange={(e) => setRename(e.target.value)}
                        aria-label="New title"
                        data-testid="objective-rename-input"
                      />
                      <button
                        type="submit"
                        className={styles.objEditBtn}
                        disabled={busyKey !== null || !rename.trim()}
                        data-testid="objective-rename-save"
                      >
                        RENAME
                      </button>
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        onClick={() => setRenaming(null)}
                        data-testid="objective-rename-cancel"
                      >
                        CANCEL
                      </button>
                    </form>
                  ) : null}
                  {onOps ? (
                    <span className={styles.objEdit}>
                      {/* REORDER. The route takes the whole key list, in the order it should end
                          up — so a move is computed here and posted as one `reorder` op, never as
                          a pair of swaps that could half-apply. Ends are disabled rather than
                          wrapping: a control that silently moves a row to the far end is worse
                          than one that says it cannot move. */}
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        disabled={busyKey !== null || i === 0}
                        onClick={() =>
                          void run(o.key, [
                            { op: "reorder", keys: moved(i, -1) },
                          ])
                        }
                        data-testid="objective-up"
                        aria-label={`Move "${o.title ?? o.key}" up`}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        disabled={
                          busyKey !== null || i === objectives.length - 1
                        }
                        onClick={() =>
                          void run(o.key, [
                            { op: "reorder", keys: moved(i, 1) },
                          ])
                        }
                        data-testid="objective-down"
                        aria-label={`Move "${o.title ?? o.key}" down`}
                      >
                        ↓
                      </button>
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        disabled={busyKey !== null}
                        onClick={() => {
                          setRenaming(o.key);
                          setRename(o.title ?? "");
                        }}
                        data-testid="objective-rename"
                        aria-label={`Rename "${o.title ?? o.key}"`}
                      >
                        RENAME
                      </button>
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        disabled={busyKey !== null || settled}
                        onClick={() =>
                          void run(o.key, [{ op: "waive", key: o.key }])
                        }
                        data-testid="objective-waive"
                        // Spelled out because "waive" reads as jargon and the distinction it
                        // carries is the whole point: this says the objective was not required,
                        // NOT that it was observed to hold.
                        aria-label={`Mark "${o.title ?? o.key}" not required`}
                      >
                        NOT REQUIRED
                      </button>
                      <button
                        type="button"
                        className={styles.objEditBtn}
                        disabled={busyKey !== null}
                        onClick={() =>
                          void run(o.key, [{ op: "drop", key: o.key }])
                        }
                        data-testid="objective-drop"
                        aria-label={`Remove "${o.title ?? o.key}"`}
                      >
                        REMOVE
                      </button>
                    </span>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {onOps ? (
        <form
          className={styles.objAddRow}
          onSubmit={add}
          aria-label="Add an objective"
        >
          <input
            className={styles.objAddInput}
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            placeholder="Add an objective…"
            aria-label="New objective"
            data-testid="objective-add-input"
          />
          <button
            type="submit"
            className={styles.objEditBtn}
            disabled={busyKey !== null || !adding.trim()}
            data-testid="objective-add"
          >
            ADD
          </button>
        </form>
      ) : null}
    </>
  );
}
