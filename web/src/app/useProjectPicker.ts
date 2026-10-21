import { useEffect, useState, useSyncExternalStore } from "react";
import { api } from "../lib/api";
import { projectPickerStore, type ProjectPickerEntity } from "../lib/projectPickerStore";

export type ProjectPicker = {
  /** The cached list (archived projects excluded); `null` until one has ever loaded. */
  projects: ProjectPickerEntity[] | null;
  /** THIS mount's revalidation has settled — succeeded or failed — for the current generation. */
  fresh: boolean;
  /** …and it failed: `projects` is the cached list (or `null`), not a confirmed one. */
  failed: boolean;
};

/** The New session form's project list (#1436): paints the cached list at once, and revalidates on
 *  every mount and after every project mutation. `fresh` is what the form gates Start on, so a
 *  selection the server has since dropped is never launched from the cache. */
export function useProjectPicker(): ProjectPicker {
  const projects = useSyncExternalStore(projectPickerStore.subscribe, projectPickerStore.list);
  const gen = useSyncExternalStore(projectPickerStore.subscribe, projectPickerStore.generation);
  const [settled, setSettled] = useState<{ gen: number; ok: boolean } | null>(null);
  useEffect(() => {
    let live = true;
    api
      .projectPickerEntities()
      .then((r) => {
        projectPickerStore.commit(
          gen,
          r.projects.filter((p) => !p.archived),
        );
        if (live) setSettled({ gen, ok: true });
      })
      .catch(() => {
        if (live) setSettled({ gen, ok: false });
      });
    return () => {
      live = false;
    };
  }, [gen]);
  const fresh = settled?.gen === gen;
  return { projects, fresh, failed: fresh && !settled.ok };
}
