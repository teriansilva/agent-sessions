import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../lib/api";
import { sessionPathFromKey } from "../../lib/format";
import type { AskSubagent } from "../../types/api";
import s from "./SubagentGroup.module.css";

const STATE_WORDS: Record<string, string> = {
  working: "working",
  waiting: "waiting for an answer",
  done: "done",
  taken_over: "you took over",
  mission: "a mission holds it",
  gone: "not available",
  starting: "starting",
  unreadable: "can't be read right now",
  queued: "queued",
  not_started: "not started",
  cancelled: "cancelled",
  asks: "its card waits for you",
  started: "starting",
};

function stateOf(r: AskSubagent): string {
  return r.state ?? r.outcome;
}

function live(r: AskSubagent): boolean {
  const st = stateOf(r);
  return (
    st === "working" ||
    st === "queued" ||
    st === "started" ||
    st === "asks" ||
    st === "starting" ||
    st === "unreadable"
  );
}

/** A task's subagents as ONE group under its turn (#2398): each with its specialist version and
 *  agent, its state, how often HQ steered it, and the Commander's own controls — Open, Take
 *  over (opens the session: typing there takes it over), Hand back (A7's route), Stop (the
 *  session's own interrupt route, as the operator), and Cancel on a queued spawn. Every control
 *  is the Commander's tap through their signed-in routes, never an HQ act. */
export function SubagentGroup({
  cid,
  turnId,
  rows,
  taskN,
  onChanged,
}: {
  cid: string;
  turnId: string;
  /** The conversation's subagents with their live states (`useLiveSubagents`, one poller for
   *  every group in the conversation). */
  rows: AskSubagent[];
  taskN?: number;
  /** One of the Commander's taps settled: read the live states again now. */
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const shown = rows.filter((r) => r.turn_id === turnId);
  const anyLive = shown.some(live);

  if (shown.length === 0) return null;

  const act = async (id: string, fn: () => Promise<unknown>, failed: string) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(`${failed}: ${e instanceof Error ? e.message : "no answer"}`);
    } finally {
      if (alive.current) setBusy(null);
      onChanged();
    }
  };

  const running = shown.filter((r) => stateOf(r) === "working").length;
  return (
    <section
      className={s.group}
      data-testid="ask-subagents"
      aria-label={`Subagents of task ${taskN ?? ""}`.trim()}
    >
      <div className={s.head}>
        <span className={s.k}>
          Subagents{taskN ? ` · task ${taskN}` : ""} · {shown.length} of them
        </span>
        <span className={s.st} data-testid="ask-subagents-state">
          {running ? "Running" : anyLive ? "Waiting" : "Settled"}
        </span>
      </div>
      <ul className={s.list}>
        {shown.map((r) => {
          const st = stateOf(r);
          const path = r.key ? sessionPathFromKey(r.key) : null;
          const takenOver = st === "taken_over";
          const canHandBack =
            takenOver &&
            r.key &&
            r.driver?.here &&
            typeof r.driver.generation === "number";
          const id = `${r.turn_id}:${r.handle}`;
          return (
            <li
              key={id}
              className={s.row}
              data-testid="ask-subagent"
              data-handle={r.handle}
              data-state={st}
            >
              <div className={s.text}>
                <div className={s.name}>
                  {r.agent || "Subagent"}
                  {r.title ? <span className={s.where}> · {r.title}</span> : null}
                </div>
                <div className={s.meta} data-testid="ask-subagent-meta">
                  <span>{r.handle}</span>
                  <span> · {STATE_WORDS[st] ?? st}</span>
                  {r.steered ? <span> · steered {r.steered}×</span> : null}
                  {r.reason && (st === "queued" || st === "not_started" || st === "asks") ? (
                    <span> · {r.reason}</span>
                  ) : null}
                </div>
              </div>
              <div className={s.actions}>
                {path ? (
                  <button
                    type="button"
                    className={s.ghost}
                    onClick={() => navigate(path)}
                    data-testid="ask-subagent-open"
                  >
                    {takenOver || st === "done" || st === "gone" ? "Open" : "Take over"}
                  </button>
                ) : null}
                {canHandBack ? (
                  <button
                    type="button"
                    className={s.ghost}
                    disabled={busy === id}
                    onClick={() =>
                      void act(
                        id,
                        () => api.handBackDriver(r.key!, r.driver!.generation as number),
                        "Not handed back",
                      )
                    }
                    data-testid="ask-subagent-hand-back"
                  >
                    Hand back
                  </button>
                ) : null}
                {st === "working" && r.key && r.active_turn ? (
                  <button
                    type="button"
                    className={s.stop}
                    disabled={busy === id}
                    onClick={() =>
                      void act(
                        id,
                        () =>
                          api.structuredInterrupt(r.key!, crypto.randomUUID(), r.active_turn!),
                        "Not stopped",
                      )
                    }
                    data-testid="ask-subagent-stop"
                  >
                    Stop
                  </button>
                ) : null}
                {st === "queued" ? (
                  <button
                    type="button"
                    className={s.ghost}
                    disabled={busy === id}
                    onClick={() =>
                      void act(
                        id,
                        () => api.askCancelSubagent(cid, r.turn_id, r.handle),
                        "Not cancelled",
                      )
                    }
                    data-testid="ask-subagent-cancel"
                  >
                    Cancel
                  </button>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
      {error ? (
        <div className={s.error} role="alert" data-testid="ask-subagents-error">
          {error}
        </div>
      ) : null}
    </section>
  );
}
