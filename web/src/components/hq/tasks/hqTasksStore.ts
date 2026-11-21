/** HQ's task list (#2103), shared: ONE poll for every reader (the strip in HQ's page and
 *  sidebar, the shell's working mark), like `askUnreadStore`.
 *
 *  `GET /api/hq/tasks` on the first subscription, then on a timer whose pace follows what the
 *  list says (closely while anything runs or waits, slowly otherwise), when the tab comes back,
 *  and at once when something here changed a task (`announceHqTasks`: a send, a Stop, a
 *  Withdraw, a settle). Only the NEWEST read applies. A failed read keeps what is shown. The
 *  last reader to leave stops the poll and forgets the list. */
import { useSyncExternalStore } from "react";

import { api } from "../../../lib/api";
import type { HqTaskList } from "../../../types/api";
import { EMPTY_TASKS, tasksPollMs } from "./hqTasks";

/** Something changed a task: every reader reads again now, and once more shortly after (a turn
 *  just sent may not have reached the server yet). */
export const HQ_TASKS_EVENT = "agent-sessions:hq-tasks";
const SETTLE_MS = 1_500;

export interface HqTasksSnapshot {
  list: HqTaskList;
  /** This page's clock when `list` was read: a running task's time ticks from it. */
  readAt: number;
  /** A first read has landed. */
  loaded: boolean;
}

let snapshot: HqTasksSnapshot = { list: EMPTY_TASKS, readAt: 0, loaded: false };
const readers = new Set<() => void>();
let gen = 0;
let timer: number | null = null;
let again: number | null = null;

function emit(): void {
  for (const fn of readers) fn();
}

function apply(list: HqTaskList): void {
  snapshot = { list, readAt: Date.now(), loaded: true };
  emit();
  arm();
}

function arm(): void {
  if (timer !== null) window.clearTimeout(timer);
  timer = readers.size ? window.setTimeout(read, tasksPollMs(snapshot.list.counts)) : null;
}

function read(): void {
  if (readers.size === 0) return;
  if (document.visibilityState !== "visible") {
    arm();
    return;
  }
  const g = ++gen;
  Promise.resolve()
    .then(() => api.hqTasks())
    .then((list) => {
      if (g !== gen || readers.size === 0) return;
      apply({ ...EMPTY_TASKS, ...list });
    })
    .catch(() => {
      if (g === gen) arm(); // keep what is shown; try again at the usual pace
    });
}

function onEvent(): void {
  read();
  if (again !== null) window.clearTimeout(again);
  again = window.setTimeout(() => {
    again = null;
    read();
  }, SETTLE_MS);
}

function subscribe(fn: () => void): () => void {
  const first = readers.size === 0;
  readers.add(fn);
  if (first) {
    document.addEventListener("visibilitychange", read);
    window.addEventListener(HQ_TASKS_EVENT, onEvent);
    read();
  }
  return () => {
    if (!readers.delete(fn) || readers.size > 0) return;
    gen += 1;
    if (timer !== null) window.clearTimeout(timer);
    if (again !== null) window.clearTimeout(again);
    timer = again = null;
    snapshot = { list: EMPTY_TASKS, readAt: 0, loaded: false };
    document.removeEventListener("visibilitychange", read);
    window.removeEventListener(HQ_TASKS_EVENT, onEvent);
  };
}

const getSnapshot = () => snapshot;
const serverSnapshot: HqTasksSnapshot = { list: EMPTY_TASKS, readAt: 0, loaded: false };

/** HQ's tasks, read while anything is mounted that shows them. */
export function useHqTasks(): HqTasksSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, () => serverSnapshot);
}

/** Tell every reader a task changed. */
export function announceHqTasks(): void {
  window.dispatchEvent(new Event(HQ_TASKS_EVENT));
}

/** Stop every running HQ task and withdraw every queue; the answer is the list as it is now. */
export async function stopAllHqTasks(): Promise<void> {
  const list = await api.hqStopAllTasks();
  gen += 1; // a read sent before this must not put the stopped tasks back
  if (readers.size) apply({ ...EMPTY_TASKS, ...list });
  announceHqTasks();
}
