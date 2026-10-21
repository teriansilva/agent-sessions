import type { StructuredModelList } from "../types/api";
import { ApiError, api } from "./api";

/** One API client's model list as the browser last saw it (#1503). */
export interface StructuredModelsSnapshot {
  /** The last list the server answered for this engine; `null` until one ever arrived. */
  list: StructuredModelList | null;
  /** `Date.now()` when `list` arrived: its age is `fetched_age_s` plus the time since. */
  receivedAt: number;
  /** A request for this engine is in flight (the cached list stays on screen meanwhile). */
  revalidating: boolean;
  /** The newest request failed while `list` is an older answer: why. Cleared by a success. */
  error: string | null;
}

/** API clients' model lists, kept in module memory and keyed by engine (#1503).
 *
 *  The shape of `projectPickerStore`: it outlives every route, so returning to the New session
 *  form — or switching between API clients on it — paints the last list at once, and every use
 *  revalidates behind it. It is a RENDERING cache: the server validates every create against the
 *  current binary's own list, so a list this cache still shows after a CLI upgrade is refused there
 *  with the server's reason, never trusted. It is never persisted; a reload drops it.
 *
 *  Each engine's requests are single-flight, and each commit is generation-guarded: a forced
 *  revalidation (Retry, a refused create) supersedes one in flight, and the older answer is dropped
 *  when it lands. A failed request never replaces a list: it is kept, and the failure is noted. */
const EMPTY: StructuredModelsSnapshot = {
  list: null,
  receivedAt: 0,
  revalidating: false,
  error: null,
};
/** The server answered "stale" — it is refreshing behind that answer — so ask again every few
 *  seconds until it is not, for at most `STALE_RECHECK_WINDOW_MS`. The window outlasts the
 *  server's slowest refresh (#1503 PR review): a probe's 20 s deadline (`native_models.TIMEOUT_S`)
 *  plus its kill, behind up to two other clients' probes on the one refresher, plus margin. */
export const STALE_RECHECK_MS = 3000;
export const STALE_RECHECK_WINDOW_MS = 90_000;

const snapshots = new Map<string, StructuredModelsSnapshot>();
const generations = new Map<string, number>();
const inflight = new Map<string, Promise<void>>();
const rechecks = new Map<string, { timer: ReturnType<typeof setTimeout>; since: number }>();
const listeners = new Set<() => void>();

const emit = () => listeners.forEach((l) => l());
const put = (engine: string, patch: Partial<StructuredModelsSnapshot>) => {
  snapshots.set(engine, { ...(snapshots.get(engine) ?? EMPTY), ...patch });
  emit();
};

function scheduleRecheck(engine: string, list: StructuredModelList) {
  const prior = rechecks.get(engine);
  if (prior) clearTimeout(prior.timer);
  if (!list.stale || list.refresh_error) {
    rechecks.delete(engine);
    return;
  }
  // The window runs from the FIRST stale answer: only a non-stale one (above) resets it.
  const since = prior?.since ?? Date.now();
  if (Date.now() - since >= STALE_RECHECK_WINDOW_MS) {
    rechecks.delete(engine);
    return;
  }
  rechecks.set(engine, {
    since,
    timer: setTimeout(() => void structuredModelsStore.revalidate(engine), STALE_RECHECK_MS),
  });
}

export const structuredModelsStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  /** A stable snapshot (the same object until it changes), as `useSyncExternalStore` needs. */
  get: (engine: string): StructuredModelsSnapshot => snapshots.get(engine) ?? EMPTY,
  generation: (engine: string): number => generations.get(engine) ?? 0,
  /** Ask the server for `engine`'s list. Joins a request already in flight unless `force`d. */
  revalidate(engine: string, { force = false }: { force?: boolean } = {}): Promise<void> {
    const running = inflight.get(engine);
    if (running && !force) return running;
    const gen = (generations.get(engine) ?? 0) + 1;
    generations.set(engine, gen);
    put(engine, { revalidating: true });
    const request = api
      .structuredModels(engine)
      .then(
        (list) => {
          structuredModelsStore.commit(engine, gen, list);
        },
        (e: unknown) => {
          structuredModelsStore.fail(
            engine,
            gen,
            e instanceof ApiError && e.message ? e.message : "it could not be asked",
          );
        },
      )
      .finally(() => {
        if (inflight.get(engine) === request) inflight.delete(engine);
      });
    inflight.set(engine, request);
    return request;
  },
  /** Store `list`, answered for the request of generation `gen` — dropped if superseded. */
  commit(engine: string, gen: number, list: StructuredModelList): boolean {
    if (gen !== generations.get(engine)) return false;
    put(engine, { list, receivedAt: Date.now(), revalidating: false, error: null });
    scheduleRecheck(engine, list);
    return true;
  },
  /** The request of generation `gen` failed: keep the last list. With none, the failure IS the
   *  answer (`unavailable` with the reason), so the form says why rather than waiting forever. */
  fail(engine: string, gen: number, reason: string): boolean {
    if (gen !== generations.get(engine)) return false;
    const prior = snapshots.get(engine);
    if (prior?.list) {
      put(engine, { revalidating: false, error: reason });
    } else {
      put(engine, {
        list: { status: "unavailable", models: [], reason },
        receivedAt: Date.now(),
        revalidating: false,
        error: null,
      });
    }
    return true;
  },
  /** Tests only: forget everything, as a page reload would. */
  reset(): void {
    rechecks.forEach((r) => clearTimeout(r.timer));
    rechecks.clear();
    snapshots.clear();
    inflight.clear();
    // Generations keep counting, so an answer to a request made before the reset is dropped.
    generations.forEach((g, e) => generations.set(e, g + 1));
    emit();
  },
};

/** How old the list on screen is, in whole minutes (at least 0). */
export function listAgeMinutes(snap: StructuredModelsSnapshot, now = Date.now()): number {
  const served = snap.list?.fetched_age_s ?? 0;
  return Math.max(0, Math.round((served + (now - snap.receivedAt) / 1000) / 60));
}
