/** A matched session's ways in, for Ask's answer rows: its full-screen route, the seed a map
 *  window opens from, and how much of a batch the map's cap admits. Pure, and in its own module so
 *  `AskConsole.tsx` exports components only (Fast Refresh). */
import type { WindowSeed } from "../overview/useWorkspace";
/** What a way in needs from a match: its key and title — a stored match (#1443) has just these. */
type MatchRef = { id: string; title: string };

/** `engine:uuid` → its two halves. */
function splitKey(key: string): { engine: string; uuid: string } {
  const i = key.indexOf(":");
  return i < 0
    ? { engine: key, uuid: "" }
    : { engine: key.slice(0, i), uuid: key.slice(i + 1) };
}

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded. */
export function matchRoute(key: string): string {
  const { engine, uuid } = splitKey(key);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** A match → the seed a map window opens from. */
export function matchSeed(m: MatchRef): WindowSeed {
  const { engine, uuid } = splitKey(m.id);
  return { key: m.id, engine, id: uuid, title: m.title || uuid };
}

/** Which of `matches` a batch "Open in map" may ask for: every one that already has a window
 *  (re-opening focuses it and costs no room), plus as many new ones as `room` admits, in the
 *  answer's order. Pure, so the arithmetic is pinned without a map. */
export function mapBatch<M extends MatchRef>(
  matches: M[],
  openKeys: ReadonlySet<string>,
  room: number,
): { take: M[]; left: number } {
  const take: M[] = [];
  let fresh = 0;
  let left = 0;
  for (const m of matches) {
    if (openKeys.has(m.id)) take.push(m);
    else if (fresh < room) {
      take.push(m);
      fresh++;
    } else left++;
  }
  return { take, left };
}
