/** A project's memory switch, from ONE place (#2143).
 *
 *  The switch is drawn twice: on the project's page (HQ → Projects → Memory & equipment) and on
 *  Library → Memory, which is the notebook. Both call the same endpoint, but a shared endpoint does
 *  not stop a stale page, so the flag each one DRAWS is read through this store:
 *
 *  - every surface that reads the server's answer (`/api/memory` or `/api/memory/<pid>`) publishes
 *    it here, and every mount re-reads it — the server stays the authority;
 *  - a toggle writes through `setProjectMemory`, which publishes the server's answer on success and
 *    re-reads the server's value on failure, so a failed save shows what is really stored;
 *  - values are kept PER PROJECT and ordered by a sequence number taken when a read or write
 *    STARTS, so a late answer for a project you have since left lands in that project's slot only,
 *    and never overwrites a newer answer for the same project.
 *
 *  Module state, not React context: the two surfaces live on different routes and never share a
 *  provider, and this is the same kind of tiny store as `hqChoiceStore`. */
import { useSyncExternalStore } from "react";
import { api } from "./api";

interface Entry {
  enabled: boolean;
  /** The sequence number of the read or write this value came from. */
  seq: number;
}

let entries: Record<string, Entry> = {};
let nextSeq = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** A sequence number for a read or write that is starting now. */
export function beginMemoryRead(): number {
  return nextSeq++;
}

/** Record the server's value for `pid`, as answered by the read or write that took `seq`. An answer
 *  older than the one already held is dropped. */
export function publishProjectMemory(pid: string, enabled: boolean, seq: number = beginMemoryRead()) {
  const held = entries[pid];
  if (held && held.seq > seq) return;
  if (held && held.enabled === enabled && held.seq === seq) return;
  entries = { ...entries, [pid]: { enabled, seq } };
  emit();
}

/** Re-read every project's flag from the server (`GET /api/memory`) and publish it. Resolves with
 *  the rows the server returned, so a caller that needs the counts too reads them once. */
export async function readProjectMemory() {
  const seq = beginMemoryRead();
  const res = await api.memoryProjects();
  for (const p of res.projects) publishProjectMemory(p.id, p.enabled, seq);
  return res.projects;
}

/** Turn a project's memory on or off. On success the server's answer is published; on failure the
 *  server's stored value is re-read and published (the switch goes back to the truth) and the error
 *  is rethrown for the caller to show. */
export async function setProjectMemory(pid: string, enabled: boolean): Promise<boolean> {
  const seq = beginMemoryRead();
  try {
    const res = await api.setMemoryEnabled(pid, enabled);
    publishProjectMemory(pid, res.enabled, seq);
    return res.enabled;
  } catch (e) {
    await readProjectMemory().catch(() => {});
    throw e;
  }
}

/** The flag this store holds for `pid`: `undefined` until a read has answered. */
export function useProjectMemory(pid: string | null): boolean | undefined {
  return useSyncExternalStore(subscribe, () => (pid ? entries[pid]?.enabled : undefined));
}

/** Tests only: forget everything. */
export function resetProjectMemoryStore() {
  entries = {};
  nextSeq = 1;
  emit();
}
