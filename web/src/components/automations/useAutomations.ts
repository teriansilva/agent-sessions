/** The automations list as a page reads it (#1201): one GET, kept through a failed refresh.
 *
 *  A failed reload keeps the rows it already has and says when they were loaded — the "load error
 *  keeping last data" state — rather than blanking the page. The page refreshes on focus and every
 *  `REFRESH_MS` while it is mounted and visible (next-run times move); nothing outside the page
 *  polls. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api";
import type { ProjectEntity } from "../../types/api";
import type { Automation, AutomationList } from "../../types/automations";

export const REFRESH_MS = 60_000;

export interface AutomationsState {
  data: AutomationList | null;
  /** When `data` was loaded, ms. */
  loadedAt: number | null;
  error: string | null;
  /** True until the first read settles, either way. */
  loading: boolean;
  /** A read's outcome, never ambiguous: `ok: false` is a FAILED read, which is not the same fact
   *  as a successful read that lacks some row (#1252 review). */
  reload: () => Promise<ReloadResult>;
  /** Apply a mutation's own authoritative row NOW, and void every read started before it. */
  applyRow: (row: Automation) => void;
}

export type ReloadResult = { ok: true; data: AutomationList } | { ok: false; error: string };

export function useAutomations(): AutomationsState {
  const [data, setData] = useState<AutomationList | null>(null);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const live = useRef(true);
  /** Only the NEWEST-started read applies (#1252 review): a slow focus or interval GET that
   *  finishes after the reload following an Enable must not paint the old state back. */
  const generation = useRef(0);

  const reload = useCallback(async (): Promise<ReloadResult> => {
    const mine = ++generation.current;
    try {
      const r = await api.automations();
      if (live.current && mine === generation.current) {
        setData(r);
        setLoadedAt(Date.now());
        setError(null);
      }
      return { ok: true, data: r };
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Couldn’t load automations";
      if (live.current && mine === generation.current) setError(msg);
      return { ok: false, error: msg };
    } finally {
      if (live.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    live.current = true;
    // The fetch's state lands after its await, never synchronously in this body.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    // A hidden tab does not poll; coming back reads once, then the interval resumes.
    const id = window.setInterval(() => {
      if (!document.hidden) void reload();
    }, REFRESH_MS);
    const onFocus = () => void reload();
    const onVisible = () => {
      if (!document.hidden) void reload();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live.current = false;
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  const applyRow = useCallback((row: Automation) => {
    generation.current += 1; // a read that started before this change can no longer apply
    // The verb responses also carry `in_flight`; the list keeps only the row's own fields.
    const { in_flight: _f, in_flight_detail: _d, ...clean } = row;
    void _f;
    void _d;
    setData((d) =>
      d
        ? {
            ...d,
            automations: d.automations.some((x) => x.id === clean.id)
              ? d.automations.map((x) => (x.id === clean.id ? { ...x, ...clean } : x))
              : [...d.automations, clean],
          }
        : d,
    );
  }, []);

  return { data, loadedAt, error, loading, reload, applyRow };
}

/** Project entities by id, for names. A failed read leaves ids showing, never an error. */
export function useProjects(): ProjectEntity[] | null {
  const [projects, setProjects] = useState<ProjectEntity[] | null>(null);
  useEffect(() => {
    let live = true;
    api
      .projectEntities()
      .then((r) => live && setProjects(r.projects ?? []))
      .catch(() => live && setProjects([]));
    return () => {
      live = false;
    };
  }, []);
  return projects;
}

/** A clock for relative times, ticking every 30 s. */
export function useNowS(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}
