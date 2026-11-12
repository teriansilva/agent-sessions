/** Names for an artifact's provenance (#1932 P2). The store records ids (`session_key`, `project_id`,
 *  `mission_id`); the page shows the session's title, the project's name and the mission's title.
 *  Projects and missions come from one list read each; a session title is read once per key the
 *  page shows, cached for the visit, at most `SESSION_READS` at a time. An id nobody could name
 *  is shown as itself, never hidden. */
import { useEffect, useMemo, useState } from "react";

import { api } from "../../lib/api";

const SESSION_READS = 40;
const sessionTitles = new Map<string, string | null>();

export interface ArtifactNames {
  project: (id: string | null | undefined) => string | null;
  mission: (id: string | null | undefined) => string | null;
  session: (key: string) => string | null;
  projects: { id: string; name: string }[];
  missions: { id: string; title: string }[];
}

export function useArtifactNames(
  sessionKeys: readonly string[],
): ArtifactNames {
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [missions, setMissions] = useState<{ id: string; title: string }[]>([]);
  const [, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .projectEntities({ includeArchived: true })
      .then(
        (r) =>
          live &&
          setProjects(r.projects.map((p) => ({ id: p.id, name: p.name }))),
      )
      .catch(() => {});
    api
      .missions({ limit: 200 })
      .then(
        (r) =>
          live &&
          setMissions(r.missions.map((m) => ({ id: m.id, title: m.title }))),
      )
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  const wanted = useMemo(
    () =>
      [...new Set(sessionKeys)]
        .filter((k) => !sessionTitles.has(k))
        .slice(0, SESSION_READS),
    [sessionKeys],
  );
  useEffect(() => {
    if (wanted.length === 0) return;
    let live = true;
    for (const k of wanted) sessionTitles.set(k, null); // in flight: read once
    Promise.all(
      wanted.map((k) =>
        api
          .session(k)
          .then((s) => sessionTitles.set(k, s.title || null))
          .catch(() => {}),
      ),
    ).then(() => live && setTick((n) => n + 1));
    return () => {
      live = false;
    };
  }, [wanted]);

  return useMemo(() => {
    const pm = new Map(projects.map((p) => [p.id, p.name]));
    const mm = new Map(missions.map((m) => [m.id, m.title]));
    return {
      project: (id) => (id ? (pm.get(id) ?? null) : null),
      mission: (id) => (id ? (mm.get(id) ?? null) : null),
      session: (key) => sessionTitles.get(key) ?? null,
      projects,
      missions,
    };
    // `session` reads the cache when called: the tick above re-renders once titles land.
  }, [projects, missions]);
}
