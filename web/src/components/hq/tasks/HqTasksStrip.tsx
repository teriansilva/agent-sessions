/** HQ's tasks strip (#2103): ONE component in three hosts — the head of HQ's conversation list
 *  (`page`), the head of the HQ sidebar (`sidebar`), and the shell's popover or sheet opened by
 *  the working mark (`topbar`).
 *
 *  A row is uniform, three lines: the state with its tally or age on the right, the question's
 *  first line, the conversation's name; each one line, ellipsised; the action on the right. A
 *  running task carries the #2076 process summary and **Stop**; a queued one **Withdraw**; one
 *  parked on a question links to its conversation with **Answer** (it has nothing to stop). The
 *  recent ones are links with a visible **Open**; one the server never started offers
 *  **Re-send**. **Stop all** shows only when more than one task could be stopped.
 *
 *  Everything shown is the server's stored first lines, titles, counts and times (`hqTasks.ts`).
 */
import { ArrowRight, X } from "lucide-react";
import { useEffect, useState, type Ref } from "react";
import { Link } from "react-router-dom";

import { api } from "../../../lib/api";
import { HQ_PATH, hqConversationPath } from "../../../lib/routes";
import type { HqTask } from "../../../types/api";
import { canStopAll, countsLine, runningElapsed, taskRow } from "./hqTasks";
import { announceHqTasks, stopAllHqTasks, useHqTasks } from "./hqTasksStore";
import s from "./HqTasks.module.css";

export type HqTasksHost = "page" | "sidebar" | "topbar";

/** How many settled tasks each host lists: the shell shows the server's whole window, HQ's own
 *  list a few, and the sidebar none (its room is the conversation's). */
const RECENT_SHOWN: Record<HqTasksHost, number> = { page: 3, sidebar: 0, topbar: 6 };

/** This page's clock, once a second, only while a task is running. */
function useNow(ticking: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [ticking]);
  return now;
}

/** Ask a question again that the server never started: the turn's own stored question, as a
 *  new turn in its conversation. */
async function resend(task: HqTask): Promise<void> {
  const c = await api.askConversation(task.cid);
  const turn = (c.turns ?? []).find((t) => t.turn_id === task.turn_id);
  if (!turn || !turn.question) throw new Error("That question is no longer there.");
  await api.askMessageStream(
    task.cid,
    { turnId: crypto.randomUUID(), text: turn.question, contact: turn.contact ?? "off" },
    () => {},
  );
}

export function HqTasksStrip({
  host,
  onNavigate,
  onClose,
  closeRef,
}: {
  host: HqTasksHost;
  /** A row's link was followed (the shell closes its popover or sheet). */
  onNavigate?: () => void;
  /** The shell's sheet: a Close in the head, always in reach. */
  onClose?: () => void;
  closeRef?: Ref<HTMLButtonElement>;
}) {
  const { list, readAt, loaded } = useHqTasks();
  const running = list.tasks.some((t) => t.state === "running");
  const now = useNow(running);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [stoppingAll, setStoppingAll] = useState(false);
  const [resent, setResent] = useState<ReadonlySet<string>>(new Set());
  const [failed, setFailed] = useState<string | null>(null);

  const act = async (key: string, fn: () => Promise<unknown>, after?: () => void) => {
    setBusy((b) => new Set(b).add(key));
    setFailed(null);
    try {
      await fn();
      after?.();
    } catch (e) {
      setFailed(e instanceof Error && e.message ? e.message : "That did not go through.");
    } finally {
      setBusy((b) => {
        const n = new Set(b);
        n.delete(key);
        return n;
      });
      announceHqTasks();
    }
  };

  const stopAll = async () => {
    setStoppingAll(true);
    setFailed(null);
    try {
      await stopAllHqTasks();
    } catch (e) {
      setFailed(e instanceof Error && e.message ? e.message : "That did not go through.");
    } finally {
      setStoppingAll(false);
    }
  };

  const recent = list.recent
    .filter((t) => !resent.has(`${t.cid}:${t.turn_id}`))
    .slice(0, RECENT_SHOWN[host]);
  // The sidebar shows the strip only while there is something to act on: its room is the
  // conversation's.
  if (host === "sidebar" && list.tasks.length === 0) return null;
  const counts = countsLine(list.counts);

  return (
    <section
      className={`${s.strip} ${s[host]}`}
      aria-label="HQ tasks"
      data-testid="hq-tasks"
      data-host={host}
    >
      <div className={s.head}>
        <h2 className={s.name}>Tasks</h2>
        <span className={s.counts} data-testid="hq-tasks-counts">
          {counts}
        </span>
        {canStopAll(list.counts) ? (
          <button
            type="button"
            className={s.stopAll}
            disabled={stoppingAll}
            onClick={() => void stopAll()}
            data-testid="hq-tasks-stop-all"
          >
            <span className={s.square} aria-hidden="true" />
            Stop all
          </button>
        ) : null}
        {onClose ? (
          <button
            ref={closeRef}
            type="button"
            className={s.close}
            onClick={onClose}
            aria-label="Close HQ tasks"
            data-testid="hq-tasks-close"
          >
            <X size={20} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {failed ? (
        <p className={s.failed} role="alert" data-testid="hq-tasks-error">
          {failed}
        </p>
      ) : null}
      {list.tasks.length === 0 ? (
        <p className={s.empty} data-testid="hq-tasks-empty">
          <span className="hud-led idle" aria-hidden="true" />
          {loaded ? "Nothing running" : "Reading…"}
        </p>
      ) : (
        <ul className={s.rows} data-testid="hq-tasks-rows">
          {list.tasks.map((t) => {
            const row = taskRow(t, now, runningElapsed(t, readAt, now));
            const waiting = busy.has(row.key);
            return (
              <li
                key={row.key}
                className={`${s.row} ${s[row.tone]}`}
                data-testid="hq-task"
                data-state={t.state}
              >
                {row.tone === "run" ? (
                  <div className={`nj-acquire ${s.bar}`} aria-hidden="true" />
                ) : null}
                <span className={`hud-led ${row.led}`} aria-hidden="true" />
                <div className={s.state}>
                  <span className={s.now}>{row.state}</span>
                  {row.tally ? <span className={s.tally}>{row.tally}</span> : null}
                </div>
                <div className={s.question}>{row.question}</div>
                <div className={s.conv}>{row.conversation}</div>
                <div className={s.action}>
                  {row.action === "stop" ? (
                    <button
                      type="button"
                      className={s.stop}
                      disabled={waiting || t.stopping}
                      onClick={() => void act(row.key, () => api.askStopTurn(t.cid, t.turn_id))}
                      aria-label={`Stop: ${row.question}`}
                      data-testid="hq-task-stop"
                    >
                      <span className={s.square} aria-hidden="true" />
                      Stop
                    </button>
                  ) : row.action === "withdraw" ? (
                    <button
                      type="button"
                      className={s.withdraw}
                      disabled={waiting}
                      onClick={() =>
                        void act(row.key, () => api.askWithdrawTurn(t.cid, t.turn_id))
                      }
                      aria-label={`Withdraw: ${row.question}`}
                      data-testid="hq-task-withdraw"
                    >
                      Withdraw
                    </button>
                  ) : (
                    <Link
                      className={s.open}
                      to={hqConversationPath(t.cid)}
                      onClick={onNavigate}
                      aria-label={`Answer in ${row.conversation}`}
                      data-testid="hq-task-answer"
                    >
                      Answer
                      <ArrowRight size={14} aria-hidden="true" />
                    </Link>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {recent.length > 0 ? (
        <>
          <h3 className={s.sub}>Recent</h3>
          <ul className={s.rows} data-testid="hq-tasks-recent">
            {recent.map((t) => {
              const row = taskRow(t, now, null);
              return (
                <li
                  key={row.key}
                  className={`${s.row} ${s.rec} ${s[row.tone]}`}
                  data-testid="hq-task-recent"
                  data-state={t.state}
                >
                  <span className="hud-led idle" aria-hidden="true" />
                  <div className={s.state}>
                    <span className={s.now}>{row.state}</span>
                    {row.tally ? <span className={s.tally}>{row.tally}</span> : null}
                  </div>
                  <div className={s.question}>{row.question}</div>
                  <div className={s.conv}>{row.conversation}</div>
                  <div className={s.action}>
                    {row.action === "resend" ? (
                      <button
                        type="button"
                        className={s.withdraw}
                        disabled={busy.has(row.key)}
                        onClick={() =>
                          void act(
                            row.key,
                            () => resend(t),
                            () => setResent((r) => new Set(r).add(row.key)),
                          )
                        }
                        aria-label={`Re-send: ${row.question}`}
                        data-testid="hq-task-resend"
                      >
                        Re-send
                      </button>
                    ) : (
                      <Link
                        className={s.open}
                        to={hqConversationPath(t.cid)}
                        onClick={onNavigate}
                        aria-label={`Open ${row.conversation}: ${row.state}`}
                        data-testid="hq-task-open"
                      >
                        Open
                        <ArrowRight size={14} aria-hidden="true" />
                      </Link>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {host === "topbar" ? (
        <div className={s.foot}>
          <Link to={HQ_PATH} onClick={onNavigate} data-testid="hq-tasks-open-hq">
            Open HQ
            <ArrowRight size={14} aria-hidden="true" />
          </Link>
        </div>
      ) : null}
    </section>
  );
}
