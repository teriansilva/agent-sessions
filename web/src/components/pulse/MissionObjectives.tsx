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
 * **THE SUPERVISOR'S READING LIVES ON THE ROW (#942).** FOLLOW-THROUGH used to be a second panel
 * printing a second list of these same objectives — `assess()` iterates the mission's own
 * objective rows, so they were always the same rows — which meant "why has nothing happened to
 * objective 3" was a cross-reference between two lists. The badge, the budget, the server's
 * refusal sentence and STAND DOWN now render on the objective they describe. What is about the
 * MISSION rather than an objective appears in the Follow-through disclosure in the console;
 * standalone callers can retain the notices above the list. See `MissionSupervisorBoard.tsx`.
 *
 * **An empty list is not the same as no objectives.** `POST /api/missions` returns before the
 * producer has run, so a mission legitimately has nothing here for a moment. `objectives_state`
 * says which case this is, and the empty state renders it — "working out what done means" while
 * pending, the named reason on `failed` / `skipped`. Rendering an unanswered question as an
 * answer is the same family of lie as the stale probe above.
 */
import { useCallback, useState } from "react";

import type {
  MissionObjective,
  MissionSupervisor,
  SupervisorObjective,
} from "../../types/api";

import {
  MissionSupervisorNotices,
  SupervisorCell,
} from "./MissionSupervisorBoard";
import styles from "./mission.module.css";
// The classifier itself, from the module that owns it — the row stamps the board it is on so a
// test can classify a row without reading its prose.
import { boardFor } from "./supervisorBoard";

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

/** The empty state, which has FIVE different meanings and must not flatten them into one.
 *
 *  The fifth is `failed` (#942 review 1): the read itself did not land. That is not "there are
 *  none" and not "we have not been told yet" — it is "we could not look", the same three-way
 *  answer the probe runner and the supervisor board already insist on, and the operator acts
 *  differently on each. Saying "No objectives yet" over a failed request is the console asserting
 *  a fact about the mission from an I/O failure. */
function EmptyObjectives({
  objectivesState,
  failed,
}: {
  objectivesState?: string | null;
  failed?: boolean;
}) {
  if (failed) {
    return (
      <div className={styles.empty} data-testid="objectives-unreadable">
        <div className={styles.emptyLead}>
          The objective list could not be read.
        </div>
        <div>
          This is not a claim that this mission has none — the request failed.
          Anything the supervisor has already said about it is still shown
          above.
        </div>
      </div>
    );
  }
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
  objectivesFailed,
  showNotices = true,
  onOps,
  supervisor,
  budget = 3,
  onStandDown,
  busy = false,
}: {
  objectives: MissionObjective[];
  /** The console renders these once in the Follow-through disclosure. */
  showNotices?: boolean;
  /** `pending` / `done` / `failed` / `skipped` from the mission row — see the module note. */
  objectivesState?: string | null;
  /** Apply operator edits. Absent ⇒ the list is read-only (an archived or closed mission).
   *  Ops are handed over as an ARRAY and posted as one batch, because the route applies them in
   *  a single transaction — a reorder that half-applied would leave an order nobody chose. */
  /** Returns whether the SERVER accepted the ops. A caller holding the operator's typing needs
   *  that answer: clearing a draft the server rejected turns the retry into a retype. */
  onOps?: (ops: ObjectiveOp[]) => Promise<boolean>;
  /** The objectives READ failed — distinct from "there are none". See `EmptyObjectives`. */
  objectivesFailed?: boolean;
  /** The supervisor's reading, folded onto the rows (#942). Absent ⇒ `assess()` could not run,
   *  which the notices say out loud rather than rendering a clean list. */
  supervisor?: MissionSupervisor;
  /** Nudges allowed per episode — the denominator of the row's counter. */
  budget?: number;
  /** Silence one objective for the episode its row was RENDERED at. Absent ⇒ read-only, which is
   *  what an archived or closed mission gets. */
  onStandDown?: (key: string, episode: number) => void;
  /** A mission-level mutation is in flight; the supervisor's own control disables with it. */
  busy?: boolean;
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

  /** The supervisor's reading, by objective key.
   *
   *  Keyed rather than zipped by position: the two lists come from one store but through two
   *  reads, so a reorder landing between them would pair every row with the wrong assessment —
   *  and a wrong `why_not` is worse than none, because it is a sentence the operator will believe.
   *  A key with no reading renders no cell, which is the honest answer for "we have not been told
   *  about this one". */
  const reading = new Map<string, SupervisorObjective>(
    (supervisor?.objectives ?? []).map((o) => [o.key, o]),
  );

  /** THE ROWS, WHICH ARE THE UNION OF THE TWO READS — not the objective list alone (#942 review 1).
   *
   *  These are two independent client reads and either can fail on its own. When `/objectives`
   *  fails while the detail succeeds, an inner join renders NOTHING: no rows, so no badges, no
   *  refusal sentences and no STAND DOWN — under a mission-level notice still saying "1 unmet
   *  gate". Before follow-through folded in, the supervisor's own board rendered from its own
   *  list and was untouched by that failure, so this is a regression the fold introduced and the
   *  union is what undoes it.
   *
   *  An assessment carries everything a row needs to exist — key, title, gate, state — so a
   *  reading with no matching objective renders as a row in its own right. It is marked
   *  `fromAssessmentOnly` and offered no EDIT controls: reorder is meaningless without the list
   *  that defines the order, and a rename or a drop aimed at a list we could not read is a write
   *  on an unknown. STAND DOWN is offered, because it is the one action that acts on the
   *  ASSESSMENT rather than on the list. */
  const extra = (supervisor?.objectives ?? []).filter(
    (o) => !objectives.some((x) => x.key === o.key),
  );
  const rows: { o: MissionObjective; fromAssessmentOnly: boolean }[] = [
    ...objectives.map((o) => ({ o, fromAssessmentOnly: false })),
    ...extra.map((s) => ({
      o: {
        mission_id: "",
        key: s.key,
        ord: 0,
        title: s.title ?? s.key,
        probe: "",
        probe_args: null,
        gate: s.gate,
        state: s.state,
        met_at: null,
        observed: null,
        source: "supervisor",
      } as MissionObjective,
      fromAssessmentOnly: true,
    })),
  ];

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

  if (rows.length === 0 && !onOps) {
    return (
      <>
        {showNotices ? (
          <MissionSupervisorNotices supervisor={supervisor} />
        ) : null}
        <EmptyObjectives
          objectivesState={objectivesState}
          failed={objectivesFailed}
        />
      </>
    );
  }

  return (
    <>
      {/* THE MISSION-LEVEL HALF, above the rows — see the module note. It renders on the empty
          case too, which is the one notice whose entire meaning is that there are no rows. */}
      {showNotices ? (
        <MissionSupervisorNotices supervisor={supervisor} />
      ) : null}
      {rows.length === 0 ? (
        <EmptyObjectives
          objectivesState={objectivesState}
          failed={objectivesFailed}
        />
      ) : (
        <ul
          style={{ listStyle: "none", margin: 0, padding: 0 }}
          aria-label="Objectives"
          data-testid="objectives"
        >
          {rows.map(({ o, fromAssessmentOnly }, i) => {
            const st = staleness(o);
            const settled = o.state === "met" || o.state === "waived";
            const sup = reading.get(o.key);
            return (
              <li
                key={o.key}
                className={styles.objRow}
                data-testid="objective"
                data-key={o.key}
                // The board the supervisor put this objective on. Stamped on the row rather than
                // only on the badge so a test can classify a row without reading its prose —
                // which is what the follow-through specs already assert against.
                {...(sup ? { "data-board": boardFor(sup) } : {})}
              >
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
                  {onOps && !fromAssessmentOnly && renaming === o.key ? (
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
                  {/* THE SUPERVISOR'S READING OF THIS OBJECTIVE (#942) — the old FOLLOW-THROUGH
                      row, on the objective it was always about. */}
                  {sup ? (
                    <SupervisorCell
                      o={sup}
                      budget={budget}
                      onStandDown={onStandDown}
                      busy={busy || busyKey !== null}
                    />
                  ) : null}
                </span>
                {/* THE THIRD COLUMN (#942). These act ON the row, so they sit at the end of it
                    rather than between the title and the supervisor's reading of it — which is
                    where folding follow-through in had left them, five boxed 44px controls on a
                    line of their own. */}
                {/* …and only for rows that came from the LIST (#942 review 1). A row rendered
                    from the assessment alone has no place in an order we could not read, and a
                    rename or a drop aimed at it would be a write against an unknown. */}
                {onOps && !fromAssessmentOnly ? (
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
                        void run(o.key, [{ op: "reorder", keys: moved(i, -1) }])
                      }
                      data-testid="objective-up"
                      aria-label={`Move "${o.title ?? o.key}" up`}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className={styles.objEditBtn}
                      disabled={busyKey !== null || i === objectives.length - 1}
                      onClick={() =>
                        void run(o.key, [{ op: "reorder", keys: moved(i, 1) }])
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
