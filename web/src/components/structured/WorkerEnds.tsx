/** Work that ended with its worker, and idle exits (#1724): transcript rows, inline until the
 *  background strip (#1726) lands. One row per task or process still active when the generation
 *  ended — ENDED WITH WORKER · name · why · when — or IDLE EXIT with how long it was idle. A row
 *  is what BattleLab last OBSERVED: work that started and ended between two observations of a
 *  worker that was killed is not listed, and no row claims it. Processes show their program
 *  name and pid only. Neither row is an active failure, so neither takes the failure colour. */
import type { StructuredWorkerEnd } from "../../types/api";
import { useArriveClass } from "./arrivals";
import pane from "./StructuredPane.module.css";
import styles from "./BackgroundWork.module.css";

const REASONS: Record<StructuredWorkerEnd["reason"], string> = {
  stop: "operator stop",
  oom: "out of memory",
  protocol: "the agent's connection ended",
  worker_ended: "worker ended",
  idle: "idle",
};

const clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const full = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

function When({ end }: { end: StructuredWorkerEnd }) {
  const date = new Date(end.at * 1000);
  if (!Number.isFinite(date.getTime())) return null;
  return (
    <time className={styles.endWhen} dateTime={date.toISOString()} title={full.format(date)}>
      {end.settled ? "" : "last seen "}
      {clock.format(date)}
    </time>
  );
}

export function WorkerEnds({ ends }: { ends: StructuredWorkerEnd[] | undefined }) {
  // A generation that ended while the pane was open arrives (#1727's rule): never on first paint.
  const arrive = useArriveClass();
  if (!ends?.length) return null;
  const rows = ends.flatMap((end) => {
    if (end.reason === "idle") {
      const minutes = Math.max(1, Math.round((end.idle_for ?? 0) / 60));
      return [
        <p key={`${end.worker}:idle`} className={`${styles.end}${arrive(`end:${end.worker}`)}`}
          data-testid="structured-worker-end"
          data-reason="idle">
          <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
          <span className={styles.endTag}>Idle exit</span>
          <span className={styles.endWhat}>
            Stopped after {minutes} min idle — no background work
          </span>
          <When end={end} />
        </p>,
      ];
    }
    const why = REASONS[end.reason];
    const names = [
      ...(end.tasks ?? []).map((t) => ({ id: `task:${t.task_id}`, name: t.description || t.task_type || t.task_id })),
      ...(end.processes ?? []).map((p) => ({
        id: `pid:${p.pid}:${p.start}`,
        name: `${p.comm} · pid ${p.pid}${p.started_during_stop ? " · started while stopping" : ""}`,
      })),
      ...(end.more ? [{ id: "more", name: `${end.more} more ${end.more === 1 ? "process" : "processes"}` }] : []),
      ...(end.scan === "unknown" ? [{ id: "unknown", name: "processes that couldn’t be listed" }] : []),
    ];
    return names.map(({ id, name }) => (
      <p key={`${end.worker}:${id}`} className={`${styles.end}${arrive(`end:${end.worker}`)}`}
        data-testid="structured-worker-end"
        data-reason={end.reason}>
        <span className={`${pane.led} ${pane.ledIdle}`} aria-hidden="true" />
        <span className={styles.endTag}>Ended with worker</span>
        <span className={styles.endWhat}>{name} · {why}</span>
        <When end={end} />
      </p>
    ));
  });
  return <div className={styles.ends}>{rows}</div>;
}
