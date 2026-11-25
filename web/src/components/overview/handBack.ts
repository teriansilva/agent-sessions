import type { WindowRequest } from "./useWorkspace";

/** Where a request the map's drain refused goes, and what it carries (#1648).
 *
 *  It is handed back to its full-screen route with its launch params (`fresh`), so the session is
 *  never lost. `mapFull` is set ONLY for a refusal at the window cap: the destination page then
 *  says "the map was full". A map that could not host a window at all (too small, a phone) is not
 *  "full", and says nothing. */
export function handBack(req: WindowRequest): { to: string; state: Record<string, unknown> } {
  return {
    to: `/s/${encodeURIComponent(req.seed.engine)}/${encodeURIComponent(req.seed.id)}`,
    state: {
      ...(req.fresh ? { fresh: req.fresh } : {}),
      ...(req.reason === "cap" ? { mapFull: true } : {}),
    },
  };
}
