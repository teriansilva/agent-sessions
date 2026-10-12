import type { SessionDefault, StructuredModelList } from "../types/api";

/** What the stored new-session defaults (#1407) mean against the client's CURRENT model list.
 *
 *  The server keeps a stored value even after the client stops offering it (lenient read); this
 *  is where it becomes "stale" instead of being preselected. The new-session screen and the
 *  agent page both read it, from the same list the create is validated against. */
export interface ResolvedDefault {
  /** The model to preselect, or null for the client's own default. */
  model: string | null;
  /** The level to preselect (only beside `model`), or null for the model's own. */
  effort: string | null;
  /** The list has not answered yet: the stored values are shown as "checking". */
  pending: boolean;
  /** The list could not be read, so a stored model cannot be offered (only `default` can start). */
  unverified: boolean;
  /** A stored model the client no longer lists. */
  modelStale: string | null;
  /** A stored level the (still listed) model no longer offers. */
  effortStale: string | null;
}

export function resolveSessionDefault(
  saved: SessionDefault | null | undefined,
  list: StructuredModelList | null,
): ResolvedDefault {
  const none: ResolvedDefault = {
    model: null,
    effort: null,
    pending: false,
    unverified: false,
    modelStale: null,
    effortStale: null,
  };
  if (!saved?.model) return none;
  if (list === null) return { ...none, model: saved.model, effort: saved.effort, pending: true };
  if (list.status !== "ok") return { ...none, unverified: true };
  const listed = list.models.find((m) => m.id === saved.model);
  if (!listed) return { ...none, modelStale: saved.model };
  if (saved.effort && !listed.efforts.includes(saved.effort)) {
    return { ...none, model: saved.model, effortStale: saved.effort };
  }
  return { ...none, model: saved.model, effort: saved.effort };
}

/** The levels a model offers, from the client's own list; none for `default` or an unknown id. */
export function levelsOf(list: StructuredModelList | null, model: string): string[] {
  if (model === "default" || list?.status !== "ok") return [];
  return list.models.find((m) => m.id === model)?.efforts ?? [];
}
