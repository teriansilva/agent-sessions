/** What HQ's page route names (#1776), read from the router-relative pathname. */
import { ASK_CID_RE, HQ_PATH } from "../../lib/routes";

/** The route as the page reads it: the list (`/hq`), one conversation (`/hq/c/<id>`), or a path
 *  that names nothing the page can open — a malformed id, or `/hq/d/…` before dispatches exist.
 *  A malformed id is decided HERE, so it asks the server nothing. */
export type HqTarget =
  | { kind: "list" }
  | { kind: "conversation"; id: string }
  | { kind: "nothing" };

export function hqTarget(pathname: string): HqTarget {
  if (pathname === HQ_PATH || pathname === `${HQ_PATH}/`) return { kind: "list" };
  const m = /^\/hq\/c\/([^/]+)\/?$/.exec(pathname);
  if (m) {
    let id: string;
    try {
      id = decodeURIComponent(m[1]);
    } catch {
      return { kind: "nothing" };
    }
    if (ASK_CID_RE.test(id)) return { kind: "conversation", id };
  }
  return { kind: "nothing" };
}
