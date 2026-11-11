/** What HQ's page route names (#1776), read from the router-relative pathname. */
import { ASK_CID_RE, HQ_DISPATCH_ID_RE, HQ_DISPATCHES_PATH, HQ_PATH } from "../../lib/routes";

/** The route as the page reads it: the list (`/hq`), one conversation (`/hq/c/<id>`), the
 *  Dispatches (`/hq/d`, or `/hq/d/<id>` linked to one, #1837), or a path that names nothing the
 *  page can open. A malformed id is decided HERE, so it asks the server nothing: a malformed
 *  dispatch id is `dispatches` with `id: null` and `malformed`, the view's own "isn't here". */
export type HqTarget =
  | { kind: "list" }
  | { kind: "conversation"; id: string }
  | { kind: "dispatches"; id: string | null; malformed: boolean }
  | { kind: "nothing" };

export function hqTarget(pathname: string): HqTarget {
  if (pathname === HQ_PATH || pathname === `${HQ_PATH}/`) return { kind: "list" };
  if (pathname === HQ_DISPATCHES_PATH || pathname === `${HQ_DISPATCHES_PATH}/`) {
    return { kind: "dispatches", id: null, malformed: false };
  }
  const d = /^\/hq\/d\/([^/]+)\/?$/.exec(pathname);
  if (d) {
    let id = "";
    try {
      id = decodeURIComponent(d[1]);
    } catch {
      /* malformed */
    }
    return HQ_DISPATCH_ID_RE.test(id)
      ? { kind: "dispatches", id, malformed: false }
      : { kind: "dispatches", id: null, malformed: true };
  }
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
