/** The mission thread's rows, drawn from `threadRow` (#967 P4, #966 P2).
 *
 *  - **Conversation stays a message**: an `article` named for its speaker, in the boxed style it had.
 *  - **A state change is a centred row**: the time, then the from and to chips between two hairlines.
 *    The chips are the header's own (`stateChip`, the rail's dot and label from `missionState.ts`), so
 *    `dispatching` reads "starting" in all three places.
 *  - **A plan is one compact row** with its brief behind Show brief; an edit names the fields it
 *    changed; a planning outcome is one quiet row with its reason.
 *  - **An `error` event is one compact row**: an alert icon and its own text in `--danger-text`, with
 *    the time. Its only writer is a mission question that could not be asked or delivered, so it has
 *    no chips and no actions, and it is a labelled group rather than a live alert.
 *  - **A start that failed is a block** with a 3px `--status-down` edge: the chips, the server's
 *    plain-language message, and the technical detail in mono under it. Only the newest failure of a
 *    mission that is still `failed` offers actions: Start again when the DETAIL allows it, Open
 *    session when the event names one, and Why no retry? otherwise. Older failures are the record.
 *
 *  Everything is rendered as text; nothing from `meta` is printed wholesale. */
import {
  ArrowRight,
  ChevronRight,
  CircleAlert,
  CirclePause,
  FileText,
  Pencil,
  RotateCw,
  Send,
  Terminal,
} from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import type {
  Mission,
  MissionEvent,
  MissionObjective,
  PulseAskMatch,
} from "../../types/api";

import action from "../ui/actionButton.module.css";
import styles from "./mission.module.css";
import { missionDotClass, missionStateLabel } from "./missionState";
import {
  latestFailedStartSeq,
  sessionRoute,
  threadRow,
  type ThreadRow,
} from "./missionThread";
import t from "./missionThread.module.css";
import d from "./direction.module.css";
import type { StartAgain } from "./useStartAgain";

function Time({ at }: { at: number }) {
  const d = new Date(at * 1000);
  return (
    <time className={t.time} dateTime={d.toISOString()}>
      {d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
    </time>
  );
}

function StateChip({ state, testId }: { state: string; testId: string }) {
  return (
    <span
      className={`${styles.stateChip} ${state === "failed" ? styles.stateChipFailed : ""} ${t.chip}`}
      data-testid={testId}
    >
      <span
        className={`${styles.dot} ${missionDotClass(state)} ${styles.chipDot}`}
        aria-hidden="true"
        data-testid="state-chip-dot"
      />
      {missionStateLabel(state)}
    </span>
  );
}

function Transition({ from, to }: { from: string; to: string }) {
  return (
    <>
      <StateChip state={from} testId="state-chip-from" />
      <ArrowRight size={13} className={t.arrow} aria-hidden="true" />
      <StateChip state={to} testId="state-chip-to" />
    </>
  );
}

function transitionName(from: string, to: string) {
  return `${missionStateLabel(from)} to ${missionStateLabel(to)}`;
}

function MessageRow({ event, who }: { event: MissionEvent; who: string }) {
  const meta = (event.meta ?? {}) as { matches?: PulseAskMatch[] };
  const matches = Array.isArray(meta.matches) ? meta.matches : [];
  const text = event.text?.trim() ? event.text : null;
  return (
    <article className={styles.event} aria-label={who}>
      <div className={styles.eventHead}>{who}</div>
      {text ? <div className={styles.eventText}>{text}</div> : null}
      {matches.map((m) => (
        <div key={m.id} className={styles.matchRow} data-testid="ask-match">
          <div className={styles.eventText}>{m.title}</div>
          {m.why ? <div className={styles.objReason}>{m.why}</div> : null}
          <Link
            className={styles.openSession}
            to={sessionRoute(m.id)}
            aria-label={`Jump into ${m.title}`}
          >
            Jump in
          </Link>
        </div>
      ))}
    </article>
  );
}

function FailureBlock({
  event,
  row,
  actions,
}: {
  event: MissionEvent;
  row: Extract<ThreadRow, { type: "failure" }>;
  actions: StartAgain | null;
}) {
  const [whyOpen, setWhyOpen] = useState(false);
  const reasonId = useId();
  const eligible = actions?.eligible === true;
  return (
    <div
      className={t.failure}
      role="group"
      aria-label={`Start failed: ${transitionName(row.from, row.to)}`}
      data-testid="thread-failure"
    >
      <div className={t.failHead}>
        <Transition from={row.from} to={row.to} />
        <Time at={event.at} />
      </div>
      <p className={t.failMessage} data-testid="thread-failure-message">
        {row.message}
      </p>
      {row.detail ? (
        <p className={t.failDetail} data-testid="thread-failure-detail">
          {row.detail}
        </p>
      ) : null}
      {actions ? (
        <>
          <div className={t.failActions} data-testid="thread-failure-actions">
            {eligible ? (
              <button
                type="button"
                className={`${action.primary} ${t.failAction}`}
                disabled={actions.disabled}
                onClick={() => void actions.run()}
                data-testid="thread-start-again"
              >
                <RotateCw size={15} aria-hidden="true" />
                Start again
              </button>
            ) : null}
            {row.sessionKey ? (
              <Link
                className={`${action.ghost} ${t.failAction}`}
                to={sessionRoute(row.sessionKey)}
                data-testid="thread-open-session"
              >
                <Terminal size={15} aria-hidden="true" />
                {eligible ? "Open session log" : "Open session"}
              </Link>
            ) : null}
            {!eligible ? (
              <button
                type="button"
                className={t.why}
                aria-expanded={whyOpen}
                aria-controls={whyOpen ? reasonId : undefined}
                onClick={() => setWhyOpen((o) => !o)}
                data-testid="thread-why-no-retry"
              >
                Why no retry?
              </button>
            ) : null}
          </div>
          {!eligible && whyOpen ? (
            <p id={reasonId} className={t.reason} data-testid="thread-retry-reason">
              {actions.reason ?? "The server did not say why this start cannot be retried."}
            </p>
          ) : null}
          {actions.error ? (
            <p className={t.error} role="alert" data-testid="thread-start-again-error">
              {actions.error}
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function PlanRow({
  event,
  row,
  projectName,
  objectiveCount,
}: {
  event: MissionEvent;
  row: Extract<ThreadRow, { type: "plan" }>;
  projectName: (id: string) => string;
  objectiveCount: number | null;
}) {
  const [open, setOpen] = useState(false);
  const briefId = useId();
  return (
    <div className={t.box} role="group" aria-label="Plan ready" data-testid="thread-plan">
      <div className={t.line}>
        <FileText size={14} className={t.icon} aria-hidden="true" />
        <span className={t.lineText}>
          Plan ready
          {row.projectId ? (
            <>
              <span className={t.sep}>·</span>
              <span className={t.mono}>{projectName(row.projectId)}</span>
            </>
          ) : null}
          {row.engine ? (
            <>
              <span className={t.sep}>·</span>
              <span className={t.mono}>{row.engine}</span>
            </>
          ) : null}
          {objectiveCount !== null ? (
            <>
              <span className={t.sep}>·</span>
              {objectiveCount} {objectiveCount === 1 ? "objective" : "objectives"}
            </>
          ) : null}
        </span>
        <Time at={event.at} />
        {row.brief ? (
          <button
            type="button"
            className={t.disclosure}
            aria-expanded={open}
            aria-controls={open ? briefId : undefined}
            onClick={() => setOpen((o) => !o)}
          >
            Show brief
            <ChevronRight
              size={13}
              aria-hidden="true"
              className={open ? t.chevronOpen : t.chevron}
            />
          </button>
        ) : null}
      </div>
      {open ? (
        <p id={briefId} className={t.brief} data-testid="thread-plan-brief">
          {row.brief}
        </p>
      ) : null}
    </div>
  );
}

/** A supervisor nudge that was typed (#983 P2, D4): one compact row naming the objective and whose
 *  words it was, with Show text revealing the delivered snapshot verbatim. */
function NudgedRow({
  event,
  row,
  objective,
}: {
  event: MissionEvent;
  row: Extract<ThreadRow, { type: "nudged" }>;
  objective: string;
}) {
  const [open, setOpen] = useState(false);
  const textId = useId();
  const whose =
    row.source === "direction"
      ? "your direction"
      : row.source === "default_nudge"
        ? "your default nudge"
        : null;
  return (
    <div
      className={t.box}
      role="group"
      aria-label={`Nudged: ${objective}`}
      data-testid="thread-nudged"
      data-source={row.source ?? ""}
    >
      <div className={t.line}>
        <Send size={14} className={t.icon} aria-hidden="true" />
        <span className={t.lineText}>
          Nudged
          <span className={t.sep}>·</span>
          <b className={d.threadStrong}>{objective}</b>
          {whose ? (
            <>
              <span className={t.sep}>·</span>
              {whose}
            </>
          ) : null}
        </span>
        <Time at={event.at} />
        <button
          type="button"
          className={t.disclosure}
          aria-expanded={open}
          aria-controls={open ? textId : undefined}
          onClick={() => setOpen((o) => !o)}
          data-testid="thread-nudged-toggle"
        >
          {open ? "Hide text" : "Show text"}
          <ChevronRight
            size={13}
            aria-hidden="true"
            className={open ? t.chevronOpen : t.chevron}
          />
        </button>
      </div>
      {open ? (
        <pre id={textId} className={d.threadTyped} data-testid="thread-nudged-text">
          {row.text}
        </pre>
      ) : null}
    </div>
  );
}

/** A supervisor nudge that was not typed (#983 P2, D4): one quiet row with the server's reason. */
function HeldRow({
  event,
  row,
  objective,
}: {
  event: MissionEvent;
  row: Extract<ThreadRow, { type: "held" }>;
  objective: string;
}) {
  return (
    <div
      className={`${t.quiet} ${t.line}`}
      role="group"
      aria-label={`Held: ${objective}`}
      data-testid="thread-held"
    >
      <CirclePause size={14} className={t.icon} aria-hidden="true" />
      <span className={`${t.lineText} ${t.note}`}>
        Held
        <span className={t.sep}>·</span>
        <b className={d.threadStrong}>{objective}</b>
        <span className={t.sep}>·</span>
        <span data-testid="thread-held-reason">{row.reason}</span>
      </span>
      <Time at={event.at} />
    </div>
  );
}

/** One timeline event in the thread. */
export function MissionThreadEvent({
  event,
  actions = null,
  projectName = (id) => id,
  objectiveCount = null,
  objectiveTitle = (key) => key ?? "an objective",
}: {
  event: MissionEvent;
  /** Set only on the newest failed start of a mission that is still failed. */
  actions?: StartAgain | null;
  projectName?: (id: string) => string;
  /** Set only on the newest plan: an older plan's checklist is not the one on screen. */
  objectiveCount?: number | null;
  /** The title of the objective a supervisor nudge was about, from the checklist on screen. */
  objectiveTitle?: (key: string | null) => string;
}) {
  const row = threadRow(event);
  let body;
  switch (row.type) {
    case "message":
      body = <MessageRow event={event} who={row.who} />;
      break;
    case "state":
      body = (
        <div
          className={t.state}
          role="group"
          aria-label={`State change: ${transitionName(row.from, row.to)}`}
          data-testid="thread-state"
        >
          <div className={t.stateLine}>
            <Time at={event.at} />
            <Transition from={row.from} to={row.to} />
          </div>
          {row.note ? <p className={t.stateNote}>{row.note}</p> : null}
        </div>
      );
      break;
    case "failure":
      body = <FailureBlock event={event} row={row} actions={actions} />;
      break;
    case "error":
      body = (
        <div
          className={`${t.quiet} ${t.line}`}
          role="group"
          aria-label="Error"
          data-testid="thread-error"
        >
          <CircleAlert size={14} className={t.errorIcon} aria-hidden="true" />
          <span className={t.errorText} data-testid="thread-error-text">
            {row.text}
          </span>
          <Time at={event.at} />
        </div>
      );
      break;
    case "plan":
      body = (
        <PlanRow
          event={event}
          row={row}
          projectName={projectName}
          objectiveCount={objectiveCount}
        />
      );
      break;
    case "plan_edit":
      body = (
        <div
          className={`${t.box} ${t.line}`}
          role="group"
          aria-label="Plan edited"
          data-testid="thread-plan-edit"
        >
          <Pencil size={14} className={t.icon} aria-hidden="true" />
          <span className={t.lineText}>
            Plan edited
            {row.changed.length ? (
              <>
                <span className={t.sep}>·</span>
                {row.changed.join(", ")}
              </>
            ) : null}
          </span>
          <Time at={event.at} />
        </div>
      );
      break;
    case "planning":
      body = (
        <div
          className={`${t.quiet} ${t.line}`}
          role="group"
          aria-label={row.label}
          data-testid="thread-planning"
          data-outcome={row.outcome}
        >
          <span className={t.tag}>{row.label}</span>
          {row.note ? <span className={`${t.lineText} ${t.note}`}>{row.note}</span> : null}
          <Time at={event.at} />
        </div>
      );
      break;
    case "nudged":
      body = (
        <NudgedRow event={event} row={row} objective={objectiveTitle(row.objectiveKey)} />
      );
      break;
    case "held":
      body = <HeldRow event={event} row={row} objective={objectiveTitle(row.objectiveKey)} />;
      break;
    default:
      body = (
        <div className={styles.event}>
          <div className={styles.eventHead}>{row.label}</div>
          {row.text ? <div className={styles.eventText}>{row.text}</div> : null}
        </div>
      );
  }
  return (
    <div data-testid="thread-event" data-kind={event.kind} data-seq={event.seq}>
      {body}
    </div>
  );
}

/** Every event on screen, in the order given, with the two "only the newest" rules applied here so
 *  the console maps nothing itself. */
export function MissionThreadEvents({
  events,
  mission,
  startAgain,
  objectives,
  projectNames,
}: {
  events: MissionEvent[];
  mission: Mission | null;
  startAgain: StartAgain;
  /** The checklist on screen, or null when it could not be read. */
  objectives: MissionObjective[] | null;
  projectNames: Record<string, string>;
}) {
  const latestFailure = latestFailedStartSeq(events);
  let latestPlan: number | null = null;
  for (const e of events)
    if (e.kind === "plan" && (latestPlan === null || e.seq > latestPlan)) latestPlan = e.seq;
  const actionable = mission?.state === "failed" && mission.archived_at == null;
  const count =
    objectives && mission?.objectives_state !== "pending" ? objectives.length : null;
  // A nudge row names its objective by the title on screen, falling back to the key the server
  // recorded; an objective since removed still reads as something rather than as nothing.
  const objectiveTitle = (key: string | null) =>
    (key ? objectives?.find((o) => o.key === key)?.title : null) || key || "an objective";
  const projectName = (id: string) =>
    projectNames[id] ??
    mission?.plan?.project_options?.find((p) => p.id === id)?.name ??
    id;
  return (
    <>
      {events.map((e) => (
        <MissionThreadEvent
          key={e.seq}
          event={e}
          actions={actionable && e.seq === latestFailure ? startAgain : null}
          projectName={projectName}
          objectiveCount={e.seq === latestPlan ? count : null}
          objectiveTitle={objectiveTitle}
        />
      ))}
    </>
  );
}
