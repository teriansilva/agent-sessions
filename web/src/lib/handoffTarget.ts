import type { EngineFamily, EngineInfo } from "../types/api";

/** Which target the Hand off dialog preselects (#2141): API first, in this order, skipping any
 *  candidate that is the source or not a handoff target on this host (`supports_seed_start`, the
 *  server's one answer):
 *
 *  1. the operator's stored default agent (#1128), resolved through its family's server-side
 *     interactive default (`families`, #1728) — a stored `claude` becomes `claude-api` when that is
 *     ready — else the stored agent itself;
 *  2. the source's OWN agent as an API session (a terminal session moves to its API client);
 *  3. the first API client in roster order;
 *  4. the first handoff target, as before.
 *
 *  The family pairing is the server's (`families`) or the API client's own manifest (`api.source`),
 *  never an engine-id list kept here. Returns null when nothing can be a target. */
export function pickHandoffTarget(
  engines: readonly EngineInfo[],
  families: readonly EngineFamily[] | undefined,
  source: string,
  stored: string | null | undefined,
): string | null {
  const enabled = engines.filter((e) => e.supports_seed_start === true);
  const can = (id: string | null | undefined): id is string =>
    !!id && id !== source && enabled.some((e) => e.id === id);
  const familyOf = (id: string) => families?.find((f) => f.clients.includes(id));
  const isApi = (id: string) => engines.find((e) => e.id === id)?.runtime === "api";

  if (stored) {
    const viaFamily = familyOf(stored)?.interactive_default.engine;
    if (can(viaFamily)) return viaFamily;
    if (can(stored)) return stored;
  }
  const own =
    familyOf(source)?.interactive_default.engine ??
    engines.find((e) => e.runtime === "api" && e.api?.source === source)?.id;
  if (can(own) && isApi(own)) return own;
  const ownApi = engines.find(
    (e) => e.runtime === "api" && e.api?.source === source && can(e.id),
  )?.id;
  if (ownApi) return ownApi;
  const firstApi = enabled.find((e) => e.runtime === "api" && e.id !== source)?.id;
  if (firstApi) return firstApi;
  return enabled.find((e) => e.id !== source)?.id ?? enabled[0]?.id ?? null;
}
