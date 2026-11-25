import type { ProjectEntity } from "../types/api";

/** A project as the launch picker sees it (#1436): `GET /api/projects?counts=0` skips the session
 *  walk that produces `session_count`, so the field is absent here — and only here. Every counted
 *  consumer keeps `ProjectEntity`, whose `session_count` stays required. */
export type ProjectPickerEntity = Omit<ProjectEntity, "session_count">;

/** The launch picker's last complete project list, kept in module memory (#1436).
 *
 *  It lives above the router in the plain sense: a module outlives every route, so returning to the
 *  New session form paints the list at once instead of a late-arriving field. It is a RENDERING
 *  cache, never a request cache: every mount revalidates (`useProjectPicker`), and nothing launches
 *  on it — the form holds Start until that revalidation settles. It is never persisted, so a reload
 *  (and a logout, which is a server redirect) drops it.
 *
 *  Kept free of `api` imports so `api.ts` can announce project mutations here without a cycle. */
let list: ProjectPickerEntity[] | null = null;
let generation = 0;
const listeners = new Set<() => void>();

const emit = () => listeners.forEach((l) => l());

export const projectPickerStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  list: (): ProjectPickerEntity[] | null => list,
  /** Bumped by every project mutation; a mounted picker refetches when it moves. */
  generation: (): number => generation,
  /** Store a list fetched at generation `gen` — dropped if a mutation landed since. */
  commit(gen: number, next: ProjectPickerEntity[]): boolean {
    if (gen !== generation) return false;
    list = next;
    emit();
    return true;
  },
  /** A project was just created (#1437): list it now, so the New session form that the wizard
   *  returns to knows its folder even if the revalidation that follows fails. */
  add(project: ProjectPickerEntity): void {
    // A list fetched before this create must not overwrite it (#1437 review): move the generation
    // on now, not only in the `invalidate` that follows the mutation's settle.
    generation += 1;
    list = [...(list ?? []).filter((p) => p.id !== project.id), project].sort(
      (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) || a.id.localeCompare(b.id),
    );
    emit();
  },
  /** A project was created, renamed, recoloured, archived, unarchived or deleted. */
  invalidate(): void {
    generation += 1;
    emit();
  },
  /** Tests only: forget everything, as a page reload would. */
  reset(): void {
    list = null;
    generation = 0;
    emit();
  },
};
