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
 * having observed it hold. The control is labelled for that meaning ("Mark not required"), never
 * "met", and the supervisor's completion proposal repeats the distinction when it quotes the
 * list back.
 *
 * **THE SUPERVISOR'S READING LIVES ON THE ROW (#942).** FOLLOW-THROUGH used to be a second panel
 * printing a second list of these same objectives — `assess()` iterates the mission's own
 * objective rows, so they were always the same rows — which meant "why has nothing happened to
 * objective 3" was a cross-reference between two lists. The badge, the budget and the server's
 * refusal sentence render on the objective they describe. What is about the MISSION rather than an
 * objective appears in the Follow-through disclosure in the console; standalone callers can
 * retain the notices above the list. See `MissionSupervisorBoard.tsx`.
 *
 * **ONE ROW, ONE MENU, ORDER BY DRAGGING (#967 P3).** A row is a drag handle, the state dot, the
 * title and ⋯, with one meta line under the title. Every action on the row — Rename, Mark not
 * required, Stand down, Move up, Move down, Remove — is in ⋯, so a row is never a strip of
 * wrapping buttons. Reordering is a drag from the handle (pointer, touch or keyboard, through
 * `ui/SortableList`), and Move up / Move down stay in the menu so it never depends on dragging.
 * Every one of those sends ONE `reorder` op with the full key list, which is what the route
 * requires. The new order shows at once and is dropped again if the server refuses it.
 *
 * **An empty list is not the same as no objectives.** `POST /api/missions` returns before the
 * producer has run, so a mission legitimately has nothing here for a moment. `objectives_state`
 * says which case this is, and the empty state renders it — "working out what done means" while
 * pending, the named reason on `failed` / `skipped`. Rendering an unanswered question as an
 * answer is the same family of lie as the stale probe above.
 */
import {
  ArrowDown,
  ArrowUp,
  CircleMinus,
  CirclePause,
  GripVertical,
  MoreHorizontal,
  Pencil,
  Plus,
  Signpost,
  Trash2,
} from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import type {
  MissionObjective,
  MissionSupervisor,
  SupervisorObjective,
} from "../../types/api";

import {
  RowMenu,
  type RowMenuEntry,
  type RowMenuItem,
} from "../sidebar/RowMenu";
import action from "../ui/actionButton.module.css";
import {
  SortableItem,
  SortableList,
  type SortableRow,
} from "../ui/SortableList";
import {
  MissionSupervisorNotices,
  SupervisorCell,
} from "./MissionSupervisorBoard";
import styles from "./mission.module.css";
import {
  clampOverflows,
  inOrder,
  moveKey,
  sharedReason,
} from "./objectiveOrder";
// The classifier itself, from the module that owns it — the row stamps the board it is on so a
// test can classify a row without reading its prose.
import { boardFor } from "./supervisorBoard";
import d from "./direction.module.css";
import { ObjectiveDirectionDialog } from "./ObjectiveDirectionDialog";
import { type DirectionOp, hasDirection } from "./objectiveDirection";

/** One `PATCH /objectives` op. The shapes the route accepts; `state` is not among them. */
export type ObjectiveOp =
  | { op: "add"; key: string; title: string; gate?: boolean }
  | { op: "drop"; key: string }
  | { op: "retitle"; key: string; title: string }
  | { op: "waive"; key: string }
  | { op: "reorder"; keys: string[] }
  /** #983: write, re-copy or remove this mission's direction for one objective. */
  | DirectionOp;

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

/** The states whose dot carries a colour or a shape of its own. Those print their state word on
 *  the meta line, because a status colour is never the only signal (design §8). An open or pending
 *  objective's dot is the neutral one, and its word is for screen readers only. */
function stateIsDrawn(o: MissionObjective): boolean {
  return o.state === "met" || o.state === "waived" || o.state === "failed";
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

/** THE TITLE: TWO LINES, AND THE REST ON REQUEST (#967 P3, Hermes on #985).
 *
 *  The row clamps its title to two lines so a list of long requirements stays scannable. A clamp
 *  with no way past it hides the end of a requirement, often the clause that matters ("…only after
 *  the report is signed off"), and on a phone, or on a finished mission with no ⋯ and no editor,
 *  nothing else can show it; a native `title` tooltip needs a hover a finger does not have.
 *
 *  So a title that is ACTUALLY clipped becomes its own disclosure: a focusable button that opens
 *  and closes the full text, on every row, read-only or not. "Actually clipped" is measured, never
 *  guessed from a character count: the observer compares the rendered text's `scrollHeight` with its
 *  clamped `clientHeight` whenever the text box changes size (a narrower column, a longer title), so
 *  a title that fits keeps plain text and gains no control.
 *
 *  It never takes part in a drag. The drag listeners and `touch-action: none` live on the handle
 *  alone (`ui/SortableList`), so a tap here toggles the text and a swipe that starts here scrolls. */
function ObjectiveTitle({
  text,
  label,
}: {
  text: string | null;
  /** The full title, for the hover tooltip. */
  label: string;
}) {
  const textRef = useRef<HTMLSpanElement>(null);
  const [clipped, setClipped] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const el = textRef.current;
    // Not while it is open: the full text never overflows, and re-measuring then would take away
    // the control that closes it again.
    if (!el || open || typeof ResizeObserver === "undefined") return;
    // An observer calls back once as it starts observing, and again whenever the box changes size.
    const ro = new ResizeObserver(() =>
      setClipped(clampOverflows(el.scrollHeight, el.clientHeight)),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, [open, text]);

  const toggle = () => setOpen((v) => !v);
  const control = clipped
    ? {
        role: "button",
        tabIndex: 0,
        "aria-expanded": open,
        "data-testid": "objective-title-toggle",
        onClick: toggle,
        onKeyDown: (e: React.KeyboardEvent) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        },
      }
    : {};
  return (
    <span
      className={
        clipped ? `${styles.objTitleBox} ${styles.objTitleToggle}` : styles.objTitleBox
      }
      {...control}
    >
      <span
        ref={textRef}
        className={
          open ? `${styles.objTitle} ${styles.objTitleOpen}` : styles.objTitle
        }
        title={label}
        data-testid="objective-title"
      >
        {text}
      </span>
      {/* The affordance, so a clipped title reads as something to open. Hidden from the accessible
          name: the button is named by the title, and `aria-expanded` says which way it is. */}
      {clipped ? (
        <span className={styles.objTitleMore} aria-hidden="true">
          {open ? "less" : "more"}
        </span>
      ) : null}
    </span>
  );
}

/** How a row takes part in the order: dragged by its handle, holding the handle's column open
 *  (a row built from the assessment alone, inside a list that can be reordered), or neither. */
type RowHandle = "drag" | "space" | "none";

interface ObjectiveRowProps {
  o: MissionObjective;
  sup: SupervisorObjective | undefined;
  budget: number;
  menu: RowMenuEntry[];
  /** The refusal sentence already said once above the list, if any. */
  shared: string | null;
  /** The inline rename form, while this row is being renamed. */
  editor: ReactNode;
}

function ObjectiveRow({
  o,
  sup,
  budget,
  menu,
  shared,
  editor,
  handle,
  // The drag wiring arrives as separate props, not one object: a node setter passed to `ref` makes
  // whatever holds it a ref to the React compiler, and reading the handle's props off that same
  // object during render is then a ref read.
  setRow,
  rowStyle,
  setHandle,
  handleProps,
  isDragging = false,
}: ObjectiveRowProps & { handle: RowHandle } & Partial<SortableRow>) {
  const st = staleness(o);
  const title = o.title ?? o.key;
  const stateText = `${o.state}${o.met_at ? ` ${when(o.met_at)}` : ""}`;
  // The state is announced in text too — the dot alone is not readable by a screen reader, and
  // status colour is load-bearing here.
  const lead = stateIsDrawn(o) ? (
    <span className={styles.objState}>{stateText}</span>
  ) : (
    <span className={styles.objSr}>{stateText}</span>
  );
  // A row that carries a direction says so on its meta line (#983 P2): a nudge for it types that
  // direction rather than the default nudge.
  const directed = hasDirection(o);
  const meta = directed ? (
    <>
      {lead}
      <span
        className={d.objDirection}
        data-testid="objective-direction-mark"
        title={
          o.direction_source === "operator"
            ? "A direction written for this mission"
            : "A direction copied from the checklist"
        }
      >
        <Signpost size={11} aria-hidden="true" />
        direction
      </span>
    </>
  ) : (
    lead
  );
  return (
    <li
      ref={setRow}
      style={rowStyle}
      className={
        isDragging
          ? `${styles.objRow} ${styles.objRowDragging}`
          : styles.objRow
      }
      data-testid="objective"
      data-key={o.key}
      // The board the supervisor put this objective on. Stamped on the row rather than only on the
      // badge so a test can classify a row without reading its prose — which is what the
      // follow-through specs already assert against.
      {...(sup ? { "data-board": boardFor(sup) } : {})}
    >
      {handle === "drag" ? (
        <button
          type="button"
          ref={setHandle}
          className={styles.objHandle}
          data-testid="objective-handle"
          {...handleProps}
          aria-label={`Reorder "${title}"`}
        >
          <GripVertical size={16} aria-hidden="true" />
        </button>
      ) : handle === "space" ? (
        <span className={styles.objHandleSpace} aria-hidden="true" />
      ) : null}
      <span
        className={`${styles.dot} ${dotFor(o)} ${styles.objDot}`}
        aria-hidden="true"
      />
      <span className={styles.objBody}>
        <ObjectiveTitle text={o.title} label={title} />
        {/* THE META LINE: the state word where the dot draws one, then the supervisor's reading of
            this objective (#942) — badge, GATE, the counter, and its sentence unless the section
            already said it once. */}
        {sup ? (
          <SupervisorCell
            o={sup}
            budget={budget}
            lead={meta}
            hideWhy={shared !== null && sup.why_not === shared}
          />
        ) : stateIsDrawn(o) || directed ? (
          <span className={styles.supCell}>{meta}</span>
        ) : (
          lead
        )}
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
        {editor}
      </span>
      {menu.length > 0 ? (
        <RowMenu
          items={menu}
          title={title}
          sheetTitle="Objective actions"
          triggerLabel={`Actions for "${title}"`}
          triggerClassName={styles.objMenuBtn}
          triggerTestId="objective-menu"
          triggerIcon={<MoreHorizontal size={18} aria-hidden="true" />}
        />
      ) : null}
    </li>
  );
}

/** A list row that can be dragged. Only these rows take part in the sort: a row outside the
 *  sortable context — read-only, or built from the assessment alone — is drawn without it. */
function SortableObjectiveRow({
  locked,
  ...props
}: ObjectiveRowProps & { locked: boolean }) {
  return (
    <SortableItem id={props.o.key} disabled={locked}>
      {(row) => <ObjectiveRow {...props} handle="drag" {...row} />}
    </SortableItem>
  );
}

/** An order the operator chose and the server has not settled. `base` is the list it was made on;
 *  once the server has ACCEPTED the order it is shown only until that list is re-read, so the
 *  re-read — not this guess — is what the operator ends up looking at. */
interface PendingOrder {
  keys: string[];
  base: MissionObjective[];
  accepted: boolean;
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
  onDirection,
  playbookId = null,
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
  /** A mission-level mutation is in flight; the row actions disable with it. */
  busy?: boolean;
  /** Send one direction op (#983 P2). Resolves to null when the server accepted it, or to its
   *  refusal in its own words, which the dialog shows. Absent ⇒ the ops go through `onOps`. */
  onDirection?: (op: DirectionOp) => Promise<string | null>;
  /** The mission's playbook, which Reset copies from. Null ⇒ no Reset is offered. */
  playbookId?: string | null;
}) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  /** Which objective's direction is being edited, by key. */
  const [directing, setDirecting] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  /** Which row is being retitled, and its draft. One at a time: a list where several rows are
   *  simultaneously mid-rename is a list whose order is hard to reason about while it moves. */
  const [renaming, setRenaming] = useState<string | null>(null);
  const [rename, setRename] = useState("");
  const [pending, setPending] = useState<PendingOrder | null>(null);
  /** The last reorder was refused. Said beside the list, because the rows snapping back is
   *  otherwise indistinguishable from a drop that did not register. */
  const [reorderRefused, setReorderRefused] = useState(false);

  const showPending =
    pending && (!pending.accepted || pending.base === objectives)
      ? pending.keys
      : null;
  const listed = inOrder(objectives, showPending);
  const keys = listed.map((o) => o.key);

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
   *  reading with no matching objective renders as a row in its own right, after the list. It is
   *  offered no EDIT actions and cannot be dragged: reorder is meaningless without the list that
   *  defines the order, and a rename or a drop aimed at a list we could not read is a write on an
   *  unknown. Stand down is offered, because it is the one action that acts on the ASSESSMENT
   *  rather than on the list. */
  const extra: MissionObjective[] = (supervisor?.objectives ?? [])
    .filter((o) => !objectives.some((x) => x.key === o.key))
    .map(
      (s) =>
        ({
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
        }) as MissionObjective,
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

  const locked = busy || busyKey !== null;

  /** Post a new order: shown at once, sent as ONE `reorder` op with every key, and dropped again if
   *  the server refuses it. A refusal is usually the list changing underneath (409) or an order
   *  that no longer names the stored keys (422); the console re-reads the list either way, so the
   *  rows settle on the server's order rather than the one the operator dragged. */
  const reorder = useCallback(
    async (next: string[]) => {
      if (!onOps || locked) return;
      setReorderRefused(false);
      setPending({ keys: next, base: objectives, accepted: false });
      const ok = await run("reorder", [{ op: "reorder", keys: next }]);
      setPending((p) =>
        p && p.keys === next ? (ok ? { ...p, accepted: true } : null) : p,
      );
      if (!ok) setReorderRefused(true);
    },
    [onOps, locked, objectives, run],
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

  /** How the drag announcements name a row: its title, quoted. */
  const nameOf = useCallback(
    (key: string) => {
      const title =
        objectives.find((o) => o.key === key)?.title ??
        supervisor?.objectives?.find((o) => o.key === key)?.title;
      return `"${title ?? key}"`;
    },
    [objectives, supervisor],
  );

  /** One direction op, answered with the server's refusal in its own words (or null). */
  const applyDirection = useCallback(
    async (op: DirectionOp): Promise<string | null> => {
      if (onDirection) return onDirection(op);
      if (!onOps) return "This mission can no longer be edited.";
      return (await onOps([op])) ? null : "That direction was not saved.";
    },
    [onDirection, onOps],
  );

  if (listed.length + extra.length === 0 && !onOps) {
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

  const shared = sharedReason(supervisor);
  const noSession = supervisor?.no_session === true;

  /** The row's ⋯ menu. Empty ⇒ no ⋯ at all, which is what a read-only row gets. */
  const menuFor = (o: MissionObjective, index: number | null): RowMenuEntry[] => {
    const title = o.title ?? o.key;
    const sup = reading.get(o.key);
    const editable = !!onOps && index !== null;
    const first: RowMenuItem[] = [];
    if (editable) {
      first.push({
        key: "rename",
        label: "Rename",
        ariaLabel: `Rename "${title}"`,
        icon: <Pencil size={15} />,
        disabled: locked,
        data: { "data-testid": "objective-rename" },
        onSelect: () => {
          setRenaming(o.key);
          setRename(o.title ?? "");
        },
      });
      first.push({
        key: "direction",
        label: "Edit direction",
        ariaLabel: `Edit direction for "${title}"`,
        icon: <Signpost size={15} />,
        disabled: locked,
        data: { "data-testid": "objective-edit-direction" },
        onSelect: () => setDirecting(o.key),
      });
      first.push({
        key: "waive",
        // Spelled out because "waive" reads as jargon and the distinction it carries is the whole
        // point: this says the objective was not required, NOT that it was observed to hold.
        label: "Mark not required",
        ariaLabel: `Mark "${title}" not required`,
        icon: <CircleMinus size={15} />,
        // Waiving something already observed to hold would replace a verified fact with a weaker
        // claim.
        disabled: locked || o.state === "met" || o.state === "waived",
        data: { "data-testid": "objective-waive" },
        onSelect: () => void run(o.key, [{ op: "waive", key: o.key }]),
      });
    }
    // "Stop telling me about this one" (#889). Hidden once the objective is already stood down —
    // HELD is the state this produces, so offering it again would suggest a second thing to do that
    // does not exist — and on a settled objective, where silencing it has no effect. DISABLED, with
    // its reason, while the mission holds no session: there is nothing to nudge, so nothing to
    // stand down from. It sends the episode the row was RENDERED at; a stale tap is a 409, never a
    // silenced report nobody has seen.
    if (onStandDown && sup && !sup.stood_down && !sup.met) {
      first.push({
        key: "stand-down",
        label: "Stand down",
        ariaLabel: noSession
          ? `Stop following up on "${title}" (no session to nudge)`
          : `Stop following up on "${title}"`,
        hint: noSession ? "No session to nudge" : undefined,
        icon: <CirclePause size={15} />,
        disabled: locked || noSession,
        data: {
          "data-testid": "objective-stand-down",
          "data-episode": sup.episode,
        },
        onSelect: () => onStandDown(o.key, sup.episode),
      });
    }
    if (!editable || index === null) return first;
    return [
      ...first,
      "separator",
      // Ends are disabled rather than wrapping: a control that silently moves a row to the far end
      // is worse than one that says it cannot move.
      {
        key: "up",
        label: "Move up",
        ariaLabel: `Move "${title}" up`,
        icon: <ArrowUp size={15} />,
        disabled: locked || index === 0,
        data: { "data-testid": "objective-up" },
        onSelect: () => void reorder(moveKey(keys, index, index - 1)),
      },
      {
        key: "down",
        label: "Move down",
        ariaLabel: `Move "${title}" down`,
        icon: <ArrowDown size={15} />,
        disabled: locked || index === keys.length - 1,
        data: { "data-testid": "objective-down" },
        onSelect: () => void reorder(moveKey(keys, index, index + 1)),
      },
      "separator",
      {
        key: "drop",
        label: "Remove",
        ariaLabel: `Remove "${title}"`,
        icon: <Trash2 size={15} />,
        danger: true,
        disabled: locked,
        data: { "data-testid": "objective-drop" },
        onSelect: () => void run(o.key, [{ op: "drop", key: o.key }]),
      },
    ];
  };

  const editorFor = (o: MissionObjective): ReactNode =>
    onOps && renaming === o.key ? (
      <form
        className={styles.objAddRow}
        onSubmit={(e) => {
          e.preventDefault();
          const title = rename.trim();
          if (!title) return;
          void run(o.key, [{ op: "retitle", key: o.key, title }]).then(
            (ok) => ok && setRenaming(null),
          );
        }}
        aria-label={`Rename "${o.title ?? o.key}"`}
      >
        <input
          className={styles.objAddInput}
          value={rename}
          onChange={(e) => setRename(e.target.value)}
          aria-label="New title"
          data-testid="objective-rename-input"
          // Opened from the menu, which returns focus to ⋯ as it closes; the field is where the
          // operator is about to type.
          autoFocus
        />
        <button
          type="submit"
          className={action.ghost}
          disabled={busyKey !== null || !rename.trim()}
          data-testid="objective-rename-save"
        >
          Save
        </button>
        <button
          type="button"
          className={action.ghost}
          onClick={() => setRenaming(null)}
          data-testid="objective-rename-cancel"
        >
          Cancel
        </button>
      </form>
    ) : null;

  const rowProps = (o: MissionObjective, index: number | null) => ({
    o,
    sup: reading.get(o.key),
    budget,
    menu: menuFor(o, index),
    shared,
    editor: editorFor(o),
  });

  const sortable = !!onOps && listed.length > 0;
  const list = (
    <ul
      className={styles.objList}
      aria-label="Objectives"
      data-testid="objectives"
    >
      {listed.map((o, i) =>
        sortable ? (
          <SortableObjectiveRow key={o.key} {...rowProps(o, i)} locked={locked} />
        ) : (
          <ObjectiveRow key={o.key} {...rowProps(o, null)} handle="none" />
        ),
      )}
      {extra.map((o) => (
        <ObjectiveRow
          key={o.key}
          {...rowProps(o, null)}
          handle={sortable ? "space" : "none"}
        />
      ))}
    </ul>
  );

  return (
    <>
      {/* THE MISSION-LEVEL HALF, above the rows — see the module note. It renders on the empty
          case too, which is the one notice whose entire meaning is that there are no rows. */}
      {showNotices ? (
        <MissionSupervisorNotices supervisor={supervisor} />
      ) : null}
      {/* SAID ONCE (#967 P3). With no session every row carried the same sentence; the section
          says it here and a row keeps only a reason that differs. Still the server's words. */}
      {shared && listed.length + extra.length > 0 ? (
        <div className={styles.objNotice} data-testid="objectives-shared-reason">
          {shared}
        </div>
      ) : null}
      {reorderRefused ? (
        <div
          className={styles.objRefused}
          role="alert"
          data-testid="objectives-reorder-refused"
        >
          That reorder did not apply. The list changed, so it shows the saved
          order again.
        </div>
      ) : null}
      {listed.length + extra.length === 0 ? (
        <EmptyObjectives
          objectivesState={objectivesState}
          failed={objectivesFailed}
        />
      ) : sortable ? (
        <SortableList
          ids={keys}
          label={nameOf}
          onMove={(from, to) => void reorder(moveKey(keys, from, to))}
        >
          {list}
        </SortableList>
      ) : (
        list
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
            className={action.ghost}
            disabled={busyKey !== null || !adding.trim()}
            data-testid="objective-add"
          >
            <Plus size={15} aria-hidden="true" />
            Add
          </button>
        </form>
      ) : null}
      {/* Only on a mission that can still be edited, and only while the objective still exists: a
          re-read that drops it closes the dialog rather than editing a row that is gone. */}
      {onOps && directing
        ? (() => {
            const target = objectives.find((o) => o.key === directing);
            return target ? (
              <ObjectiveDirectionDialog
                key={target.key}
                objective={target}
                canReset={!!playbookId}
                onApply={applyDirection}
                onClose={() => setDirecting(null)}
              />
            ) : null;
          })()
        : null}
    </>
  );
}
