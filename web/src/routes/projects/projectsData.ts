/** The reads behind HQ → Projects (#2143). Each source is read on its own and fails on its own:
 *  a list that cannot be read says so in place (with Retry) while everything else keeps working,
 *  and a failed read is never shown as an empty one ("Empty is never an error"). */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { readProjectMemory } from "../../lib/projectMemoryStore";
import type { ProjectEntity } from "../../types/api";
import type { MemoryProject } from "../../types/memory";

/** One read's state: `null` data while the first read is out, `error` set when the LAST read
 *  failed (the previous data, if any, is kept — the page says it may be stale). */
export interface Read<T> {
  data: T | null;
  error: string | null;
  retry: () => void;
}

/** The server's own words for a failed read, or a fallback — ONE clause with no closing full stop
 *  (every caller ends the sentence), and the HTTP status said once: appended only when the message
 *  does not already carry it. */
export function readError(e: unknown, fallback: string): string {
  const clause = (s: string) => s.trim().replace(/[.\s]+$/, "");
  if (e instanceof ApiError) {
    const text = clause(e.message && e.message !== String(e.status) ? e.message : fallback);
    return e.status && !text.includes(String(e.status)) ? `${text} (HTTP ${e.status})` : text;
  }
  return clause(fallback);
}

/** A read that starts on mount and on `retry()`. A newer read always wins over an older one. */
export function useRead<T>(load: () => Promise<T>, fallback: string): Read<T> & { reload: () => Promise<T | null> } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  // Every state change happens when the read SETTLES (never synchronously in the effect).
  const reload = useCallback(() => {
    const mine = ++seq.current;
    return load().then(
      (next) => {
        if (live.current && mine === seq.current) {
          setData(next);
          setError(null);
        }
        return next;
      },
      (e) => {
        if (live.current && mine === seq.current) setError(readError(e, fallback));
        return null;
      },
    );
  }, [load, fallback]);
  useEffect(() => {
    void reload();
  }, [reload]);
  const retry = useCallback(() => void reload(), [reload]);
  return { data, error, retry, reload };
}

/** Every project, archived ones included (the index holds the Archived disclosure and Unarchive). */
export function useProjectEntities() {
  return useRead(
    useCallback(() => api.projectEntities({ includeArchived: true }).then((d) => d.projects), []),
    "The server didn’t answer the project list.",
  );
}

/** Each project's memory row (flag + note counts), read through the shared memory store so the
 *  switch on any surface agrees with it. */
export function useMemoryRows() {
  return useRead<MemoryProject[]>(
    useCallback(() => readProjectMemory(), []),
    "The memory store didn’t answer.",
  );
}

/** A project's playbook, as the fleet read reports it: which playbook, its state, and whether a
 *  newer revision waits (`update_available`). The fleet read never runs verify. */
export interface ProjectPlaybookInfo {
  playbookId: string;
  name: string;
  /** The playbook's current version label, if it declares one. */
  version: string | null;
  state: string;
  updateAvailable: boolean;
}

/** project id → its playbook, from `GET /api/playbooks` plus one `GET …/projects` per playbook.
 *  A project with no deployment has no entry. One playbook's fleet read failing fails the whole
 *  column (a partial column would read as "No playbook" for the ones it missed). */
export function usePlaybookFleet() {
  return useRead<Record<string, ProjectPlaybookInfo>>(
    useCallback(async () => {
      const list = await api.playbooks();
      const fleets = await Promise.all(
        list.playbooks.filter((p) => p.ok).map(async (p) => ({ p, fleet: await api.playbookProjects(p.id) })),
      );
      const out: Record<string, ProjectPlaybookInfo> = {};
      for (const { p, fleet } of fleets) {
        for (const row of fleet.projects) {
          out[row.project_id] = {
            playbookId: p.id,
            name: p.name || p.id,
            version: p.version ?? null,
            state: row.state,
            updateAvailable: row.update_available,
          };
        }
      }
      return out;
    }, []),
    "The playbook store didn’t answer.",
  );
}

/** The folders discovery offers (unfiltered: curation surfaces offer every discovered dir). */
export function useDiscoveredFolders(key: string) {
  return useRead(
    // `key` re-runs the read when the discovery scope changes (#470).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useCallback(() => api.folders().then((d) => d.folders), [key]),
    "Discovery didn’t answer.",
  );
}

/** Active and archived projects, each in the server's order (as Settings → Projects listed them). */
export function splitProjects(all: ProjectEntity[]) {
  return {
    active: all.filter((e) => !e.archived),
    archived: all.filter((e) => e.archived),
  };
}
