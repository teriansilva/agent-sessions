/** Report answers that may have reached the agent, kept across remounts (#1889).
 *
 *  A tap whose answer may have been typed LOCKS that report's options (#1882). The lock lives here,
 *  keyed by report id, for the life of the tab, not in the row's component state: a filter change
 *  or a navigation remounts the row from the retained list (painted before its refresh), and a
 *  remounted row must never offer the options again.
 *
 *  The lock is taken BEFORE the request starts (`pending`), so a row remounted while the POST is
 *  out is locked too, and it is OBSERVABLE (`useReportLock`): every mounted row re-renders when the
 *  request settles — with its warning — or when the lock is released, even if the row that made the
 *  request is long gone. It is released only on a proven safe refusal (nothing was sent). A fresh
 *  read does not release it: a read can be served before a request whose response was lost has
 *  been handled. A settled lock expires after `REPORT_LOCK_MS`, long after the server's own reads
 *  say the report is spent (its reservation is taken before anything is minted) or the report has
 *  gone from the list; a pending one never expires while its request is out. */
import { useCallback, useSyncExternalStore } from "react";

/** What the row says while its report is locked; ``open`` adds the way to the session. */
export type ReportLockNote = { text: string; open?: boolean };

export type ReportLock =
  | { state: "pending" }
  | { state: "settled"; note: ReportLockNote | null };

export const REPORT_LOCK_MS = 10 * 60_000;

const locks = new Map<string, { until: number; lock: ReportLock }>();
const listeners = new Set<() => void>();

function notify() {
  for (const l of listeners) l();
}

function prune(now: number) {
  for (const [id, l] of locks) if (l.until <= now) locks.delete(id);
}

/** A tap on ``reportId`` is about to be sent: its options lock NOW, before the request starts. */
export function pendReport(reportId: string) {
  locks.set(reportId, { until: Infinity, lock: { state: "pending" } });
  notify();
}

/** The tap settled without proof that nothing was sent: the options stay locked, with the
 *  warning the row shows (or none, for a delivered answer). */
export function settleReport(reportId: string, note: ReportLockNote | null) {
  const now = Date.now();
  prune(now);
  locks.set(reportId, { until: now + REPORT_LOCK_MS, lock: { state: "settled", note } });
  notify();
}

/** The tap provably sent nothing: the options may be offered again. */
export function releaseReport(reportId: string) {
  if (locks.delete(reportId)) notify();
}

/** The lock on ``reportId``, or undefined when its options may be offered. */
export function reportLock(reportId: string): ReportLock | undefined {
  const l = locks.get(reportId);
  if (l && l.until <= Date.now()) {
    locks.delete(reportId);
    return undefined;
  }
  return l?.lock;
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** The lock on ``reportId`` (none for ``null``), re-rendering on every change. */
export function useReportLock(reportId: string | null): ReportLock | undefined {
  const get = useCallback(() => (reportId ? reportLock(reportId) : undefined), [reportId]);
  return useSyncExternalStore(subscribe, get, get);
}

/** Tests only: forget every lock. */
export function resetReportLocks() {
  locks.clear();
  notify();
}
