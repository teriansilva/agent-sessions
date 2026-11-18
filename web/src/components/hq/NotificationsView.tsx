/** HQ // Notifications (#2146): every event once, newest first, with HQ's line at the head of the
 *  events it covers. It replaces HQ's Dispatches (#1837) at the same URL (`/hq/d`, `/hq/d/<id>`).
 *
 *  - **Two existing reads, joined client-side by event tag** (`notificationFeed.ts`): the bell's
 *    `notificationsStore` list and `GET /api/hq/dispatches` (newest 50). A live row whose bundle
 *    isn't in that list is read on its own (`GET …/<bid>`), deduplicated and bounded; a failed or
 *    pending lookup never hides the row — it heads its group from the bell's own line.
 *  - **A member with no bell row** is drawn from the dispatch's snapshot, "No longer in the bell":
 *    under All and its class, never unread, never in Mark all read's or Clear all's reach.
 *  - **Read state:** opening the view refreshes the bell's list, it never marks anything read; only
 *    Mark all read does. The dispatch `seen` cursor is sent ONCE on entry (`hq-page.md`).
 *  - **Clear all** clears the bell's rows (live ones only — its count is theirs) behind an in-place
 *    confirm; HQ's lines stay, and the confirm says how long.
 *  - `/hq/d/<id>` scrolls to that bundle once per (id, location key) and marks it LINKED; an
 *    unknown, pruned or malformed id says it isn't here (a malformed one asks nothing).
 *  - A failed notifications read says so with Retry, never the empty line. */
import { BellOff, RotateCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../../lib/api";
import { routeWords } from "../../lib/connectors";
import { relTime } from "../../lib/format";
import { HQ_DISPATCHES_PATH } from "../../lib/routes";
import { settingsPath } from "../../routes/settingsTabs";
import type { HqDispatch } from "../../types/api";
import { Chip, Disclosure } from "../hud/Controls";
import {
  buildFeed,
  bundlesToLookUp,
  MAX_BUNDLE_LOOKUPS,
  countFeed,
  filterFeed,
  type EventClass,
  type FeedEvent,
  type FeedFilter,
  type FeedItem,
} from "../pulse/notificationFeed";
import { openLabel, targetPath } from "../pulse/notificationLinks";
import { NotificationRow } from "../pulse/NotificationRow";
import {
  mutateNotifications,
  refreshNotifications,
  useNotifications,
} from "../pulse/notificationsStore";
import bell from "../pulse/NotificationBell.module.css";
import { CONTACTS } from "./contacts";
import { DISPATCH_POLL_MS, nextDispatchRead } from "./dispatchReads";
import nv from "./Notifications.module.css";

/** How each class of event reads: its LED and its word (status colour is load-bearing). */
const LOOK: Record<EventClass, { led: string; word: string; tone: string }> = {
  needs: { led: "att", word: "Needs you", tone: nv.wWarn },
  escalation: { led: "att", word: "Escalated", tone: nv.wWarn },
  automation: { led: "down", word: "Failed", tone: nv.wDown },
  other: { led: "idle", word: "Notice", tone: "" },
};

const FILTERS: { id: FeedFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "unread", label: "Unread" },
  { id: "needs", label: "Needs you" },
  { id: "escalation", label: "Escalations" },
  { id: "automation", label: "Automations" },
];

/** The retention HQ's lines are kept under (`hq_dispatch.py`): whichever comes first. */
const RETENTION = "kept up to 30 days, at most 2,000";

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function day(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}

/** Newest first by `(at, id)`: the server's own order for the cursor. */
const newer = (a: HqDispatch, b: HqDispatch) =>
  b.at - a.at || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0);

interface ListRead {
  /** The last READABLE list (newest first), or null before one landed. */
  list: HqDispatch[] | null;
  /** The newest applied read said the store can't be read (the last list is kept). */
  unreadable: boolean;
  /** The newest read failed outright. */
  failed: boolean;
  /** The number the applied read took when it STARTED (`dispatchReads.ts`). */
  seq: number;
}

/** HQ's newest 50 lines: read on mount, then every `DISPATCH_POLL_MS` while the tab is visible
 *  (read at once when it comes back). Only a read that STARTED later replaces what is shown, so an
 *  older answer never rolls the list back; a failure keeps the last list and says so. */
function useDispatchList(): [ListRead, () => void] {
  const [read, setRead] = useState<ListRead>({
    list: null,
    unreadable: false,
    failed: false,
    seq: 0,
  });
  const applied = useRef(0);
  const live = useRef(true);
  const refresh = useCallback(() => {
    const seq = nextDispatchRead();
    api.hqDispatches().then(
      (l) => {
        if (!live.current || seq <= applied.current) return;
        if (!l || !Array.isArray(l.dispatches)) {
          setRead((p) => ({ ...p, failed: true }));
          return;
        }
        applied.current = seq;
        setRead((p) =>
          l.unreadable
            ? { ...p, unreadable: true, failed: false, seq }
            : { list: l.dispatches, unreadable: false, failed: false, seq },
        );
      },
      () => {
        if (live.current && seq > applied.current)
          setRead((p) => ({ ...p, failed: true }));
      },
    );
  }, []);
  useEffect(() => {
    live.current = true;
    refresh();
    let timer: ReturnType<typeof setTimeout> | null = null;
    const arm = () => {
      if (!document.hidden) timer = setTimeout(tick, DISPATCH_POLL_MS);
    };
    const tick = () => {
      timer = null;
      refresh();
      arm();
    };
    const onVis = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
      } else if (!timer) tick();
    };
    arm();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      live.current = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refresh]);
  return [read, refresh];
}

/** A dispatch read on its own: the linked one, or a live row's bundle beyond the newest 50.
 *  `seq` is the number its read took when it STARTED: a newer answer is never replaced by an
 *  older one. */
interface Extra {
  rec: HqDispatch | null;
  missing: boolean;
  seq: number;
}

export function NotificationsView({
  linkedId,
  malformed,
  openKey,
  configured,
}: {
  /** `/hq/d/<id>`'s id, or null for the whole view. */
  linkedId: string | null;
  /** The route held something that is not a dispatch id: "isn't here", asking nothing. */
  malformed: boolean;
  /** The location key: a linked bundle is scrolled to once per (selection, key). */
  openKey: string;
  configured: boolean;
}) {
  const { items, unread, settled, uncertain, loaded, failed } =
    useNotifications();
  const [dispatches, refreshDispatches] = useDispatchList();
  const [extras, setExtras] = useState<Record<string, Extra>>({});
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [arming, setArming] = useState(false);

  // Entering the view is opening the bell: a fresh read, nothing marked read.
  useEffect(() => {
    void refreshNotifications();
  }, []);

  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const listed = useMemo(
    () => new Set((dispatches.list ?? []).map((d) => d.id)),
    [dispatches.list],
  );
  const linked = malformed ? null : linkedId;
  // Bundles live rows name that neither the list nor an earlier lookup holds: bounded, deduped.
  const want = useMemo(
    () => bundlesToLookUp(items, new Set([...listed, ...Object.keys(extras)])),
    [items, listed, extras],
  );
  const wantKey = want.join(",");

  /** Lookups still out: id → when they started. One out for over two poll intervals is presumed
   *  hung and asked again. */
  const inflight = useRef(new Map<string, number>());
  const answered =
    dispatches.list !== null || dispatches.unreadable || dispatches.failed;
  useEffect(() => {
    if (!answered) return;
    const now = Date.now();
    const ids: string[] = [];
    // The linked bundle is re-checked against every newer list it isn't in: pruned, it isn't here.
    const ex = extras[linked ?? ""];
    if (
      linked &&
      !listed.has(linked) &&
      !ex?.missing &&
      !(ex && ex.seq > dispatches.seq)
    )
      ids.push(linked);
    // Every other bundle read on its own is re-checked against each newer READABLE list it isn't
    // in (Hermes 8452): absence from the newest 50 proves nothing, but a 404 does, and evicts it.
    // Without this, one fetched for a link that was since left, or for a row since gone, would
    // outlive its pruning for as long as the view stays open.
    if (!dispatches.unreadable)
      for (const [id, e] of Object.entries(extras))
        if (
          e.rec &&
          !listed.has(id) &&
          e.seq < dispatches.seq &&
          !ids.includes(id)
        )
          ids.push(id);
    for (const id of wantKey ? wantKey.split(",") : [])
      if (!ids.includes(id)) ids.push(id);
    // Bounded: the linked one first, at most MAX_BUNDLE_LOOKUPS per pass.
    for (const id of ids.slice(0, MAX_BUNDLE_LOOKUPS)) {
      const t = inflight.current.get(id);
      if (t !== undefined && now - t < 2 * DISPATCH_POLL_MS) continue;
      const seq = nextDispatchRead();
      inflight.current.set(id, now);
      const settle = (next: Extra) =>
        setExtras((p) => (p[id] && p[id].seq > seq ? p : { ...p, [id]: next }));
      api.hqDispatch(id).then(
        (one) => {
          inflight.current.delete(id);
          if (mounted.current)
            settle({ rec: one.dispatch, missing: false, seq });
        },
        (e) => {
          inflight.current.delete(id);
          // Only a 404 is an answer; anything else is asked again by a later poll.
          if (mounted.current && e instanceof ApiError && e.status === 404)
            settle({ rec: null, missing: true, seq });
        },
      );
    }
    // `extras` re-runs it after every answer; the guards above keep that from asking again.
  }, [
    answered,
    linked,
    listed,
    dispatches.seq,
    dispatches.unreadable,
    wantKey,
    extras,
  ]);

  const recs = useMemo(() => {
    const all = [...(dispatches.list ?? [])];
    for (const [id, e] of Object.entries(extras))
      if (e.rec && !listed.has(id)) all.push(e.rec);
    return all;
  }, [dispatches.list, extras, listed]);

  // `seen` ONCE, on entry: the newest line the view received, once the list (and a linked
  // bundle's own lookup) answered. Clamped server-side; a line written later still counts as new.
  const seenSent = useRef(false);
  const linkedSettled =
    !linked || listed.has(linked) || Boolean(extras[linked]);
  useEffect(() => {
    if (seenSent.current || dispatches.list === null || !linkedSettled) return;
    seenSent.current = true;
    const newest = [...recs].sort(newer)[0];
    if (newest) void api.hqDispatchesSeen(newest.id).catch(() => undefined);
  }, [dispatches.list, linkedSettled, recs]);

  const feed = useMemo(
    () => (loaded ? buildFeed(items, recs) : []),
    [loaded, items, recs],
  );
  const counts = useMemo(() => countFeed(feed), [feed]);
  const shown = useMemo(() => filterFeed(feed, filter), [feed, filter]);

  const missing =
    malformed ||
    Boolean(linked && !listed.has(linked) && extras[linked]?.missing);
  const linkedItem =
    !missing && linked
      ? (feed.find((i) => i.kind === "bundle" && i.id === linked) ?? null)
      : null;

  // Scroll to the linked bundle ONCE per selection, never again when a poll hands back new objects.
  const linkedRef = useRef<HTMLElement | null>(null);
  const scrolledFor = useRef("");
  const selection = linked ? `${openKey}:${linked}` : "";
  const linkedShown =
    linkedItem !== null &&
    shown.some((i) => i.kind === "bundle" && i.id === linked);
  useEffect(() => {
    if (!selection || !linkedShown || scrolledFor.current === selection) return;
    scrolledFor.current = selection;
    linkedRef.current?.scrollIntoView({ block: "center" });
  }, [selection, linkedShown]);

  const dismissOne = useCallback(async (id: string) => {
    try {
      await mutateNotifications(
        () => api.dismissNotifications({ ids: [id] }),
        false,
      );
    } catch {
      /* the row stays; a failed delete must never look like a success */
    }
  }, []);
  const markAll = useCallback(async () => {
    try {
      await mutateNotifications(() => api.markNotificationsRead(), false);
    } catch {
      /* leave the count as it is rather than lying about it */
    }
  }, []);
  const clearAll = useCallback(async () => {
    try {
      await mutateNotifications(
        () => api.dismissNotifications({ all: true }),
        true,
      );
    } catch {
      /* leave the list alone rather than pretending it cleared */
    } finally {
      setArming(false);
    }
  }, []);
  const clearSettled = useCallback(async () => {
    const ids = settled.map((s) => s.id);
    if (!ids.length) return;
    try {
      await api.clearSettledNotifications(ids);
      await refreshNotifications();
    } catch {
      /* leave the history visible rather than pretending it cleared */
    }
  }, [settled]);

  const event = (e: FeedEvent, member: boolean) => {
    const look = LOOK[e.cls];
    if (e.kind === "live") {
      const to = targetPath(e.n);
      return (
        <NotificationRow
          key={e.key}
          testId="notification-row"
          title={e.n.title}
          reason={e.n.reason}
          project={e.n.project}
          engine={e.n.engine}
          ts={e.n.ts}
          unread={!e.n.read}
          member={member}
          kind={<Kind look={look} unread={!e.n.read} />}
          open={to}
          openText={openLabel(to)}
          onDismiss={() => void dismissOne(e.n.id)}
        />
      );
    }
    return (
      <NotificationRow
        key={e.key}
        testId="notification-row"
        title={e.m.title}
        reason={e.m.reason}
        ts={e.m.ts}
        member={member}
        gone
        kind={<Kind look={look} unread={false} />}
        open={e.m.link}
        openText={e.m.link ? openLabel(e.m.link) : undefined}
      />
    );
  };

  const item = (i: FeedItem) => {
    if (i.kind === "solo")
      return (
        <ul className={`${bell.list} ${nv.solo}`}>{event(i.event, false)}</ul>
      );
    const isLinked = linkedItem !== null && i.id === linked;
    const rec = recs.find((d) => d.id === i.id) ?? null;
    const portrait = CONTACTS.find((c) => c.name === i.head.contact) ?? null;
    const words = `${i.head.count} ${i.head.count === 1 ? "event" : "events"}`;
    const panel = (
      <section
        ref={isLinked ? linkedRef : undefined}
        className={`nj-panel is-sm ${nv.bundle} ${isLinked ? nv.hit : ""}`}
        aria-label={`HQ dispatch${i.head.at ? ` at ${clock(i.head.at)}` : ""}, ${words}`}
        data-testid="hq-dispatch"
        data-dispatch-id={i.id}
        data-linked={isLinked ? "true" : undefined}
      >
        <div className={`nj-face ${nv.face}`}>
          <div className={`${nv.hq} ${portrait ? "" : nv.hqBare}`}>
            {portrait ? (
              <img
                className={nv.portrait}
                src={portrait.poster}
                alt=""
                aria-hidden="true"
              />
            ) : null}
            <div className={nv.by}>
              <span className={nv.src}>
                {i.head.contact ?? i.head.by} wrote · {words}
                {i.head.at ? ` · ${clock(i.head.at)}` : ""}
              </span>
              {isLinked ? (
                <span
                  className="nj-stamp is-hud"
                  data-testid="hq-dispatch-linked"
                >
                  Linked
                </span>
              ) : null}
            </div>
            <p className={nv.line} data-testid="hq-dispatch-line">
              {i.head.line}
            </p>
            {rec?.routes && rec.routes.length > 0 ? (
              <p className={nv.routes} data-testid="hq-dispatch-routes">
                Sent to{" "}
                {rec.routes.map((r, ri) => (
                  <span key={r.instance} data-testid="hq-dispatch-route">
                    {ri > 0 ? "; " : ""}
                    {r.connector || "a removed connector"} · {routeWords(r)}
                  </span>
                ))}
              </p>
            ) : null}
          </div>
          <ul className={bell.list}>{i.events.map((e) => event(e, true))}</ul>
        </div>
      </section>
    );
    return isLinked ? (
      <div className={`nj-glow ${nv.glow}`}>{panel}</div>
    ) : (
      panel
    );
  };

  const seps = shown.map((i, k) =>
    k === 0 || day(i.ts) !== day(shown[k - 1].ts) ? day(i.ts) : null,
  );
  const unknown = !loaded;
  // The all-clear needs the NEWEST read to have succeeded (Hermes 8452): an older empty answer is
  // never shown as "nothing needs you" beside a failed refresh; the failure line says it all.
  const allClear = loaded && !failed;
  return (
    <section
      className={nv.page}
      aria-labelledby="hq-notifications-title"
      data-testid="hq-notifications"
    >
      <header className={nv.head}>
        <span className={`nj-kicker ${nv.kicker}`}>
          <span className="nj-notch" aria-hidden="true" />
          HQ // <b>Notifications</b>
        </span>
        <h1 id="hq-notifications-title" className="nj-h1">
          Notifications
        </h1>
        <p className={nv.lede}>
          Everything that needed you, newest first. Where HQ wrote about a group
          of events, its line sits on top of them.
        </p>
      </header>
      <div className={nv.wrap}>
        {missing ? (
          <div className={nv.missing} data-testid="hq-dispatch-missing">
            <h2 className={nv.missingTitle}>This dispatch isn&apos;t here</h2>
            <p className={nv.missingText}>
              HQ&apos;s lines are {RETENTION}, so it may have been pruned, or
              the link is not one HQ made. Everything still here is below.
            </p>
            <Link
              className={nv.allLink}
              to={HQ_DISPATCHES_PATH}
              data-testid="hq-dispatches-all"
            >
              All notifications
            </Link>
          </div>
        ) : linkedItem && linkedItem.kind === "bundle" ? (
          <div
            className={nv.landed}
            role="status"
            data-testid="hq-dispatch-landed"
          >
            <span>
              Opened from a link
              {linkedItem.head.at
                ? ` · dispatch ${clock(linkedItem.head.at)}`
                : ""}
            </span>
            <Link
              className={nv.allLink}
              to={HQ_DISPATCHES_PATH}
              data-testid="hq-dispatches-all"
            >
              All notifications
            </Link>
          </div>
        ) : null}
        {!configured ? (
          <p className={nv.note} data-testid="hq-dispatches-not-set-up">
            HQ needs an AI endpoint to write its lines. Lines already written
            stay readable. Set one up in{" "}
            <Link to={settingsPath("ai-endpoint")}>
              Settings → Endpoint &amp; model
            </Link>
            .
          </p>
        ) : null}
        <div className={nv.bar}>
          <div className={nv.filters} role="group" aria-label="Show">
            {FILTERS.map((f) => (
              <Chip
                key={f.id}
                on={filter === f.id}
                onClick={() => setFilter(f.id)}
                count={unknown ? "–" : counts[f.id]}
              >
                <span data-testid={`notifications-filter-${f.id}`}>
                  {f.label}
                </span>
              </Chip>
            ))}
          </div>
          {!unknown && (unread > 0 || items.length > 0) ? (
            <div className={nv.tools}>
              {arming ? (
                <>
                  <span
                    className={nv.confirm}
                    data-testid="notifications-clear-confirm"
                  >
                    Clear {items.length}{" "}
                    {items.length === 1 ? "notification" : "notifications"} from
                    the bell? HQ&apos;s lines stay in this view ({RETENTION}).
                  </span>
                  <button
                    type="button"
                    className={bell.confirmYes}
                    onClick={() => void clearAll()}
                  >
                    Yes, clear
                  </button>
                  <button
                    type="button"
                    className={bell.tool}
                    onClick={() => setArming(false)}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  {unread > 0 ? (
                    <>
                      <span
                        className={bell.count}
                        data-testid="notifications-unread"
                      >
                        <span className={bell.countDot} aria-hidden="true" />
                        {unread} unread
                      </span>
                      <button
                        type="button"
                        className={bell.tool}
                        onClick={() => void markAll()}
                      >
                        Mark all read
                      </button>
                    </>
                  ) : null}
                  {items.length > 0 ? (
                    <button
                      type="button"
                      className={bell.tool}
                      onClick={() => setArming(true)}
                    >
                      Clear all
                    </button>
                  ) : null}
                </>
              )}
            </div>
          ) : null}
        </div>
        {failed ? (
          <p
            className={nv.note}
            role="alert"
            data-testid="notifications-failed"
          >
            Couldn&apos;t read the notifications.
            {loaded ? " Showing what was last read." : ""}
            <button
              type="button"
              className={bell.tool}
              onClick={() => void refreshNotifications()}
            >
              <RotateCw size={13} aria-hidden="true" /> Retry
            </button>
          </p>
        ) : null}
        {dispatches.unreadable ? (
          <p className={nv.note} data-testid="hq-dispatches-unreadable">
            HQ&apos;s lines can&apos;t be read right now. Notifications are
            unaffected.
          </p>
        ) : dispatches.failed ? (
          <p className={nv.note} data-testid="hq-dispatches-failed">
            Couldn&apos;t read HQ&apos;s lines. Notifications are unaffected.
            <button
              type="button"
              className={bell.tool}
              onClick={refreshDispatches}
            >
              <RotateCw size={13} aria-hidden="true" /> Retry
            </button>
          </p>
        ) : null}
        {unknown ? (
          failed ? null : (
            <p className={nv.quiet}>Reading notifications…</p>
          )
        ) : feed.length === 0 ? (
          allClear ? (
            <p className={bell.empty} data-testid="notifications-empty">
              <BellOff
                size={22}
                aria-hidden="true"
                className={bell.emptyIcon}
              />
              Nothing needs you right now.
            </p>
          ) : null
        ) : shown.length === 0 ? (
          <p className={nv.quiet} data-testid="notifications-filter-empty">
            Nothing under this filter.
          </p>
        ) : (
          <div className={nv.feed} data-testid="notifications-feed">
            {shown.map((i, k) => (
              <div
                key={i.kind === "bundle" ? `b:${i.id}` : i.event.key}
                className={nv.item}
              >
                {seps[k] ? <div className={nv.daysep}>{seps[k]}</div> : null}
                {item(i)}
              </div>
            ))}
          </div>
        )}
        {uncertain > 0 ? (
          <p
            className={bell.uncertain}
            role="status"
            data-testid="notifications-uncertain"
          >
            {uncertain} {uncertain === 1 ? "decision" : "decisions"} could not
            be read — the store is unavailable, so they cannot be acted on yet.
          </p>
        ) : null}
        {settled.length > 0 ? (
          <Disclosure
            summary={`Recent decisions (${settled.length})`}
            testId="notifications-settled"
          >
            <ul className={bell.list}>
              {settled.map((s) => (
                <li key={s.id} className={`${bell.row} ${bell.settledRow}`}>
                  <div className={bell.title}>{s.title}</div>
                  <div className={bell.foot}>
                    {s.project ? (
                      <span className={bell.proj}>{s.project}</span>
                    ) : null}
                    <span className={bell.age}>{relTime(s.ts)}</span>
                  </div>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className={bell.settledClear}
              onClick={() => void clearSettled()}
            >
              Clear recent decisions
            </button>
          </Disclosure>
        ) : null}
      </div>
    </section>
  );
}

function Kind({
  look,
  unread,
}: {
  look: { led: string; word: string; tone: string };
  unread: boolean;
}) {
  return (
    <>
      <span className={`hud-led ${look.led}`} aria-hidden="true" />
      <span className={look.tone}>{look.word}</span>
      {unread ? <span className="nj-sr-only">, unread</span> : null}
    </>
  );
}
