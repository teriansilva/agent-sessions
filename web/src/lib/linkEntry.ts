/** What a BattleLab link that arrived from OUTSIDE the app points at (#1232).
 *
 *  "Outside" is the caller's job: the initial document load, a PWA launch delivered through
 *  `window.launchQueue`, or a hand-off from a new tab (`linkHandoff.ts`). In-app navigation never
 *  goes through here, so clicking a session in the sidebar never prompts.
 *
 *  The shapes are deliberately conservative. This decides only whether to ASK how to open a link
 *  and whether another tab may be told to open it — the server's `parse_key` / `MISSION_ID_RE`
 *  remain the gate on what the id can reach. Anything that does not classify is `null`, and a
 *  `null` from a hand-off message is dropped rather than navigated to. */
import { MISSION_ID_RE } from "./missionLink";
import { MISSION_PATH } from "./routes";

export type LinkEntry =
  | { kind: "session"; engine: string; id: string; path: string }
  | { kind: "mission"; id: string; path: string };

const ENGINE_RE = /^[a-z][a-z0-9-]{0,31}$/;
const NATIVE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

/** Classify a same-origin path (+ search). Absolute URLs are accepted only when their origin is
 *  `origin`; anything else is `null`. */
export function classifyLink(raw: string, origin: string): LinkEntry | null {
  let url: URL;
  try {
    url = new URL(raw, origin);
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;
  const session = /^\/s\/([^/]+)\/([^/]+)\/?$/.exec(url.pathname);
  if (session) {
    const [, engine, id] = session;
    // A `new-<uuid>` placeholder names a launch that has not reconciled; it can never be attached
    // to from a link.
    if (!ENGINE_RE.test(engine) || !NATIVE_ID_RE.test(id) || id.startsWith("new-")) return null;
    return { kind: "session", engine, id, path: `/s/${engine}/${id}` };
  }
  if (url.pathname === MISSION_PATH || url.pathname === `${MISSION_PATH}/`) {
    const m = url.searchParams.get("m");
    if (m === null || !MISSION_ID_RE.test(m)) return null;
    return { kind: "mission", id: m, path: `${MISSION_PATH}?m=${m}` };
  }
  return null;
}
