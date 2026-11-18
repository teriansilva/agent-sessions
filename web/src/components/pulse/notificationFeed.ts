/** One list of events (#2146): the bell's live rows joined with HQ's dispatches by EVENT TAG.
 *
 *  Pure, so the precedence and order rules are pinned by unit tests rather than by rendering:
 *
 *  - **A member with a live bell row is shown AS that row** (live read state, its ✕, its Open).
 *    Which member a row is comes from the tag both projections expose (`dispatch.tag` on the row,
 *    `tag` on the member) — never from matching titles, which are mutable and guarded text.
 *  - **Only members without one fall back to the dispatch's snapshot** (`gone`): dismissed,
 *    retracted or evicted past the bell's window — the view cannot tell which, so it says "No
 *    longer in the bell". A snapshot is never unread and never in a bulk action's count.
 *  - **Bundles sort by their newest event; events inside one sort newest first.** Two events for
 *    the same session are two tags, so two rows.
 *  - A live row whose bundle the caller could not read (not in the newest 50 and its own lookup
 *    failed or is still out) is NEVER hidden: it heads its group from the line the bell's own join
 *    gave it. */
import type {
  HqDispatch,
  HqDispatchMember,
  PulseNotification,
} from "../../types/api";

export type EventClass = "needs" | "escalation" | "automation" | "other";

export type FeedEvent =
  | {
      kind: "live";
      key: string;
      cls: EventClass;
      ts: number;
      n: PulseNotification;
    }
  | {
      kind: "gone";
      key: string;
      cls: EventClass;
      ts: number;
      m: HqDispatchMember;
    };

export interface FeedHead {
  line: string;
  /** The short signature ("Gen. Okafor"), always present. */
  by: string;
  /** The contact's full name, when the dispatch itself was read. */
  contact: string | null;
  /** When the line was written, when the dispatch itself was read. */
  at: number | null;
  /** How many events the dispatch is about. */
  count: number;
}

export type FeedItem =
  | {
      kind: "bundle";
      id: string;
      ts: number;
      head: FeedHead;
      events: FeedEvent[];
    }
  | { kind: "solo"; ts: number; event: FeedEvent };

export type FeedFilter =
  "all" | "unread" | "needs" | "escalation" | "automation";

const TAG_CLASS: [string, EventClass][] = [
  ["needs-you:", "needs"],
  ["escalation:", "escalation"],
  ["automation:", "automation"],
];

function classOfTag(tag: string | undefined): EventClass | null {
  if (!tag) return null;
  for (const [prefix, cls] of TAG_CLASS) if (tag.startsWith(prefix)) return cls;
  return null;
}

/** Which filter a live row files under: its event tag's class when HQ joined one, else the flags
 *  the row's producer stored. A row that is none of the three is under All (and Unread) only. */
export function classOfRow(n: PulseNotification): EventClass {
  const byTag = classOfTag(n.dispatch?.tag);
  if (byTag) return byTag;
  if (!n.session_id && /^automation:/.test(n.action_id ?? ""))
    return "automation";
  if (n.needs_you === true) return "needs";
  if (n.escalation === true) return "escalation";
  return "other";
}

export function classOfMember(m: HqDispatchMember): EventClass {
  const byTag = classOfTag(m.tag);
  if (byTag) return byTag;
  if (m.class === "needs you") return "needs";
  if (m.class === "mission escalation") return "escalation";
  if (m.class === "automation failed") return "automation";
  return "other";
}

const newestFirst = (
  a: { ts: number; key: string },
  b: { ts: number; key: string },
) => b.ts - a.ts || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

const itemKey = (i: FeedItem) =>
  i.kind === "bundle" ? `b:${i.id}` : i.event.key;

/** The feed: every live row once, every dispatch once, each dispatch's line at the head of the
 *  events it covers. `dispatches` may repeat an id (the newest-50 list, then single reads): the
 *  first copy wins, so pass the list first. */
export function buildFeed(
  items: PulseNotification[],
  dispatches: HqDispatch[],
): FeedItem[] {
  const recs = new Map<string, HqDispatch>();
  for (const d of dispatches) if (!recs.has(d.id)) recs.set(d.id, d);

  const liveBy = new Map<string, PulseNotification[]>();
  const out: FeedItem[] = [];
  for (const n of items) {
    const ev: FeedEvent = {
      kind: "live",
      key: `n:${n.id}`,
      cls: classOfRow(n),
      ts: n.ts,
      n,
    };
    const bid = n.dispatch?.id;
    if (!bid) {
      out.push({ kind: "solo", ts: n.ts, event: ev });
      continue;
    }
    const rows = liveBy.get(bid);
    if (rows) rows.push(n);
    else liveBy.set(bid, [n]);
  }

  const bids = new Set<string>([...recs.keys(), ...liveBy.keys()]);
  for (const bid of bids) {
    const rec = recs.get(bid) ?? null;
    const live = liveBy.get(bid) ?? [];
    const covered = new Set(
      live.map((n) => n.dispatch?.tag).filter((t): t is string => !!t),
    );
    const events: FeedEvent[] = live.map((n) => ({
      kind: "live",
      key: `n:${n.id}`,
      cls: classOfRow(n),
      ts: n.ts,
      n,
    }));
    (rec?.members ?? []).forEach((m, i) => {
      // A member whose tag a live row carries IS that row. One with no tag (the server could not
      // vouch for it) can't be matched, so it is drawn only when no live row could be it.
      const isLive = m.tag ? covered.has(m.tag) : live.length > 0;
      if (isLive) return;
      events.push({
        kind: "gone",
        key: `m:${bid}:${m.tag || i}`,
        cls: classOfMember(m),
        ts: m.ts,
        m,
      });
    });
    events.sort(newestFirst);
    const first = live[0]?.dispatch;
    const head: FeedHead = rec
      ? {
          line: rec.line,
          by: rec.by,
          contact: rec.contact,
          at: rec.at,
          count: rec.members.length,
        }
      : {
          line: first?.line ?? "",
          by: first?.by ?? "HQ",
          contact: null,
          at: null,
          count: first?.bundled ?? live.length,
        };
    const ts = events.length
      ? Math.max(...events.map((e) => e.ts))
      : (rec?.at ?? 0);
    out.push({ kind: "bundle", id: bid, ts, head, events });
  }
  return out.sort((a, b) =>
    newestFirst({ ts: a.ts, key: itemKey(a) }, { ts: b.ts, key: itemKey(b) }),
  );
}

export function matches(e: FeedEvent, f: FeedFilter): boolean {
  if (f === "all") return true;
  // Unread is live state: a snapshot has none, and is never counted as unread.
  if (f === "unread") return e.kind === "live" && !e.n.read;
  return e.cls === f;
}

/** The feed under one filter: a bundle keeps its line while any of its events match. */
export function filterFeed(feed: FeedItem[], f: FeedFilter): FeedItem[] {
  if (f === "all") return feed;
  const out: FeedItem[] = [];
  for (const i of feed) {
    if (i.kind === "solo") {
      if (matches(i.event, f)) out.push(i);
      continue;
    }
    const events = i.events.filter((e) => matches(e, f));
    if (events.length) out.push({ ...i, events });
  }
  return out;
}

export function countFeed(feed: FeedItem[]): Record<FeedFilter, number> {
  const c: Record<FeedFilter, number> = {
    all: 0,
    unread: 0,
    needs: 0,
    escalation: 0,
    automation: 0,
  };
  for (const i of feed) {
    for (const e of i.kind === "solo" ? [i.event] : i.events) {
      for (const f of Object.keys(c) as FeedFilter[])
        if (matches(e, f)) c[f] += 1;
    }
  }
  return c;
}

/** How many single-dispatch reads the view may have out for bundles beyond the newest 50. */
export const MAX_BUNDLE_LOOKUPS = 10;

/** The bundles live rows name that `known` (the list, plus anything already looked up) doesn't
 *  hold: deduplicated, newest row first, at most `max`. */
export function bundlesToLookUp(
  items: PulseNotification[],
  known: ReadonlySet<string>,
  max = MAX_BUNDLE_LOOKUPS,
): string[] {
  const out: string[] = [];
  for (const n of [...items].sort((a, b) => b.ts - a.ts)) {
    const bid = n.dispatch?.id;
    if (!bid || known.has(bid) || out.includes(bid)) continue;
    out.push(bid);
    if (out.length >= max) break;
  }
  return out;
}

/** The bell's order (#2146): its rows as before, except that the rows one HQ line covers sit
 *  together at the place of the newest of them, and the line is drawn ONCE, after the last. */
export function bellGroups(
  items: PulseNotification[],
): { bid: string | null; rows: PulseNotification[] }[] {
  const groups: { bid: string | null; rows: PulseNotification[] }[] = [];
  const at = new Map<string, number>();
  for (const n of items) {
    const bid = n.dispatch?.id ?? null;
    if (bid === null) {
      groups.push({ bid: null, rows: [n] });
      continue;
    }
    const i = at.get(bid);
    if (i === undefined) {
      at.set(bid, groups.length);
      groups.push({ bid, rows: [n] });
    } else groups[i].rows.push(n);
  }
  return groups;
}
