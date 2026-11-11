/** HQ's Dispatches (#1837, #1776 boards 2, 3 and 6 cut to #1746 P3a): the lines HQ wrote about
 *  notifications, read only, in HQ's thread column.
 *
 *  - Oldest first, like a thread; each dispatch lists its events with THEIR OWN Open link (the row's
 *    Open target, from the server's snapshot: a session, a mission, an automation's run history).
 *    There is no bundle-level guess.
 *  - `/hq/d/<id>` scrolls to that dispatch and marks it LINKED. An id the newest 50 don't hold is
 *    read on its own; an unknown, pruned, unwritten or malformed one is board 6's "isn't here" (a
 *    malformed id asks the server nothing).
 *  - Opening the view tells the server the newest dispatch it received has been seen. The server
 *    clamps it, so a line written while the view is open still counts as new afterwards.
 *  - Nothing here writes: to talk about a dispatch, ask in a conversation. */
import { Plus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../../lib/api";
import { HQ_DISPATCHES_PATH, HQ_PATH } from "../../lib/routes";
import { settingsPath } from "../../routes/settingsTabs";
import type { HqDispatch, HqDispatchMember } from "../../types/api";
import { nextDispatchRead } from "./dispatchReads";
import s from "./HqPage.module.css";

/** How each class of event reads: its word, its LED and the kind of thing its link opens. */
const CLASS_LOOK: Record<
  string,
  { word: string; led: string; tone: string; kind: string }
> = {
  "needs you": {
    word: "Needs you",
    led: "att",
    tone: s.wWarn,
    kind: "Session",
  },
  "mission escalation": {
    word: "Escalated",
    led: "att",
    tone: s.wWarn,
    kind: "Mission",
  },
  "automation failed": {
    word: "Failed",
    led: "down",
    tone: s.wDown,
    kind: "Automation",
  },
};
const look = (cls: string) =>
  CLASS_LOOK[cls] ?? { word: "Event", led: "idle", tone: "", kind: "Event" };

/** The label for a member's own Open target. */
function openLabel(link: string): string {
  if (link.startsWith("/s/")) return "Open session";
  if (link.startsWith("/mission/automations/")) return "Open automation";
  return "Open mission";
}

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

/** Every state carries `seq`, the number the view's own request took when it STARTED (the counter
 *  the page's poll shares, `dispatchReads.ts`): a NEWER poll settles the view whatever became of
 *  that request — still pending, failed, or unreadable (Hermes 7681 finding 3, 7743 finding 3). */
type Load =
  | { state: "loading"; seq: number }
  | { state: "unreadable"; seq: number }
  | { state: "failed"; seq: number }
  /** `base` is the view's own read. */
  | { state: "ready"; base: HqDispatch[]; seq: number };

/** The linked dispatch when the list does not hold it, verified on its own (`GET …/<bid>`).
 *  `seq` is the number the read that produced it took when it STARTED (`dispatchReads.ts`): a
 *  record from an older-started read never replaces one from a newer read (Hermes 7956). */
interface LinkedRec {
  id: string;
  rec: HqDispatch | null;
  missing: boolean;
  seq: number;
}

export function DispatchesView({
  linkedId,
  malformed,
  openKey,
  configured,
  onBack,
  onSeen,
  latest = null,
  latestSeq = 0,
  latestUnreadable = false,
}: {
  /** `/hq/d/<id>`'s id, or null for the whole view. */
  linkedId: string | null;
  /** The route held something that is not a dispatch id: "isn't here", asking nothing. */
  malformed: boolean;
  /** The location key: a linked dispatch is scrolled to once per (selection, key). */
  openKey: string;
  configured: boolean;
  onBack?: () => void;
  /** The cursor moved (or was asked to): the page re-reads its "N new". */
  onSeen: () => void;
  /** The page's latest poll (newest first), the AUTHORITATIVE current list once it is newer than
   *  the view's own read: a dispatch written while the view is open appears, a pruned one goes.
   *  Neither marks anything seen. */
  latest?: HqDispatch[] | null;
  /** The number `latest`'s request took when it started: it is authoritative only if it started
   *  after the view's own newest read (Hermes 7623, finding 2). */
  latestSeq?: number;
  /** `latest` came from a poll that could not read the store (Hermes 7678, finding 2): once it is
   *  the authoritative read, the view says the store can't be read, never "No dispatches yet". */
  latestUnreadable?: boolean;
}) {
  // No read has started yet: no poll is newer than it.
  const [load, setLoad] = useState<Load>({ state: "loading", seq: Infinity });
  const [linkedRec, setLinkedRec] = useState<LinkedRec | null>(null);
  /** The newest-started lookup of the linked dispatch: its id, the number it took when it started,
   *  and whether it has answered. */
  const lookup = useRef<{ id: string; seq: number; done: boolean } | null>(
    null,
  );
  /** Start a lookup of `id`: the number it takes orders it against every other read. */
  const startLookup = (id: string) => {
    const seq = nextDispatchRead();
    lookup.current = { id, seq, done: false };
    return seq;
  };
  /** Settle the linked record from a read that started as `seq`, unless a newer-started read
   *  already settled it for the same id. `next` null: that read's list held the dispatch. */
  const settleLinked = (next: LinkedRec | null, seq: number) => {
    if (lookup.current?.seq === seq) lookup.current.done = true;
    setLinkedRec((prev) =>
      prev && prev.id === linkedId && prev.seq > seq ? prev : next,
    );
  };
  const [retry, setRetry] = useState(0);
  /** `seen` is sent ONCE, on entry into the Dispatches (this mount), never on navigation inside
   *  them (Hermes 7620, finding 4): a dispatch that arrived while they were open stays new. */
  const seenSent = useRef(false);

  useEffect(() => {
    if (malformed) return;
    let live = true;
    void (async () => {
      const seq = nextDispatchRead();
      setLoad({ state: "loading", seq });
      try {
        const list = await api.hqDispatches();
        if (!live) return;
        if (list.unreadable) {
          setLoad({ state: "unreadable", seq });
          return;
        }
        const base = Array.isArray(list.dispatches) ? list.dispatches : [];
        let rec: LinkedRec | null = null;
        // The read that settles the linked record: this list's, or its own lookup's.
        let from = seq;
        // A newer poll that settled the view meanwhile has already looked the dispatch up.
        const superseded =
          lookup.current?.id === linkedId && lookup.current.seq > seq;
        if (linkedId && !superseded && !base.some((d) => d.id === linkedId)) {
          from = startLookup(linkedId);
          try {
            const one = await api.hqDispatch(linkedId);
            rec = {
              id: linkedId,
              rec: one.dispatch,
              missing: false,
              seq: from,
            };
          } catch (e) {
            if (!(e instanceof ApiError && e.status === 404)) throw e;
            rec = { id: linkedId, rec: null, missing: true, seq: from };
          }
        }
        if (!live) return;
        settleLinked(rec, from);
        setLoad({ state: "ready", base, seq });
        if (seenSent.current) return;
        seenSent.current = true;
        // Entering the view: the newest dispatch it RECEIVED has been seen. Clamped server-side.
        const newest = [...base, ...(rec?.rec ? [rec.rec] : [])].sort(newer)[0];
        if (newest) {
          // Best-effort: a cursor that could not be moved only leaves "N new" as it was.
          await api.hqDispatchesSeen(newest.id).catch(() => undefined);
          if (live) onSeen();
        }
      } catch {
        if (live) setLoad({ state: "failed", seq });
      }
    })();
    return () => {
      live = false;
    };
    // `onSeen` is the page's stable refresher.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkedId, malformed, retry]);

  // The current list: the page's poll when its request STARTED after the view's own newest read
  // (Hermes 7623, finding 2), else that read. Never a union, so a pruned dispatch leaves an open
  // view (Hermes 7620, finding 6); never by arrival order, so an older answer never rolls it back.
  // A newer poll that could not read the store makes the view say so (Hermes 7678, finding 2);
  // a newer one that could settles the view, whether its own read is pending, failed or could
  // not read the store (Hermes 7681 finding 3, 7743 finding 3). A poll never marks anything seen.
  const polled = latest !== null && latestSeq > load.seq;
  const shown = polled
    ? latestUnreadable
      ? "unreadable"
      : "ready"
    : load.state;
  const source =
    shown !== "ready"
      ? null
      : polled
        ? latest
        : load.state === "ready"
          ? load.base
          : null;

  // A linked dispatch the current list does not hold is re-read on its own: still there, it stays;
  // a 404 (pruned) is "isn't here".
  const inSource = Boolean(linkedId && source?.some((d) => d.id === linkedId));
  // Already read as gone: a 404 is final, so it is not asked again on every poll.
  const knownMissing = Boolean(
    linkedRec?.id === linkedId && linkedRec?.missing,
  );
  // Whenever a poll is the current list, whatever became of the view's own read (Hermes 7956: a
  // hung own read never reaches its own lookup). Not while a lookup that started AFTER this poll
  // is still out (the own read's, usually): it is the fresher answer, and if it hangs, the next
  // poll asks again. If two land, the newer-started one stands.
  useEffect(() => {
    const fresher =
      lookup.current?.id === linkedId &&
      !lookup.current.done &&
      lookup.current.seq > latestSeq;
    if (
      !linkedId ||
      source === null ||
      inSource ||
      source !== latest ||
      knownMissing ||
      fresher
    )
      return;
    let live = true;
    const seq = startLookup(linkedId);
    api.hqDispatch(linkedId).then(
      (one) => {
        if (live)
          settleLinked(
            { id: linkedId, rec: one.dispatch, missing: false, seq },
            seq,
          );
      },
      (e) => {
        if (live && e instanceof ApiError && e.status === 404)
          settleLinked({ id: linkedId, rec: null, missing: true, seq }, seq);
      },
    );
    return () => {
      live = false;
    };
    // `startLookup`/`settleLinked` read only refs and `linkedId`, already a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkedId, source, inSource, latest, latestSeq, knownMissing]);

  const ordered = useMemo(() => {
    if (source === null) return [];
    const all = [...source];
    const extra = linkedRec && linkedRec.id === linkedId ? linkedRec.rec : null;
    if (extra && !inSource) all.push(extra);
    return all.sort(newer).reverse();
  }, [source, linkedRec, linkedId, inSource]);
  const missing =
    malformed ||
    Boolean(
      linkedRec && linkedRec.id === linkedId && linkedRec.missing && !inSource,
    );
  const linked =
    !missing && linkedId
      ? (ordered.find((d) => d.id === linkedId) ?? null)
      : null;

  // Scroll to the linked dispatch ONCE per selection (its id and the location key), never again
  // when a poll hands back fresh objects (Hermes 7620, finding 5).
  const linkedRef = useRef<HTMLElement | null>(null);
  const scrolledFor = useRef("");
  const selection = linkedId ? `${openKey}:${linkedId}` : "";
  const linkedShown = linked !== null;
  useEffect(() => {
    if (!selection || !linkedShown || scrolledFor.current === selection) return;
    scrolledFor.current = selection;
    linkedRef.current?.scrollIntoView({ block: "center" });
  }, [selection, linkedShown]);

  // A day separator before the first dispatch of each day.
  const seps = ordered.map((d, i) =>
    i === 0 || day(d.at) !== day(ordered[i - 1].at) ? day(d.at) : null,
  );
  return (
    <section
      className={s.dispatches}
      aria-labelledby="hq-dispatches-title"
      data-testid="hq-dispatches"
    >
      <header className={s.dHead}>
        {onBack ? (
          <button
            type="button"
            className={s.backLink}
            onClick={onBack}
            data-testid="hq-dispatches-back"
          >
            ‹ Conversations
          </button>
        ) : null}
        <div className={s.dHeadText}>
          <div className="nj-callsign">
            <span className="nj-notch" aria-hidden="true" />
            <span className="nj-title">HQ</span>
            <span aria-hidden="true">//</span>
            <span className="nj-value">Read only</span>
          </div>
          <h2 id="hq-dispatches-title" className={s.dTitle}>
            Dispatches
          </h2>
          <p className={s.dSrc}>HQ&apos;s notes on events</p>
        </div>
      </header>
      {linked ? (
        <div className={s.landed} data-testid="hq-dispatch-landed">
          <span>Opened from a link · dispatch {clock(linked.at)}</span>
          <Link
            className={s.allLink}
            to={HQ_DISPATCHES_PATH}
            data-testid="hq-dispatches-all"
          >
            All dispatches
          </Link>
        </div>
      ) : null}
      {!configured ? (
        <p className={s.note} data-testid="hq-dispatches-not-set-up">
          HQ needs an AI endpoint to write dispatches. Dispatches already
          written stay readable. Set one up in{" "}
          <Link to={settingsPath("ai-endpoint")}>
            Settings → Endpoint &amp; model
          </Link>
          .
        </p>
      ) : null}
      <div className={s.dScroll} data-testid="hq-dispatches-scroll">
        {missing ? (
          <div className={s.missing} data-testid="hq-dispatch-missing">
            <h3 className={s.missingTitle}>This dispatch isn&apos;t here</h3>
            <p className={s.missingText}>
              It may have been pruned (dispatches are kept for 30 days), or the
              link is not one HQ made.
            </p>
            <Link className={s.newBtn} to={HQ_DISPATCHES_PATH}>
              All dispatches
            </Link>
          </div>
        ) : shown === "loading" ? (
          <p className={s.empty}>Reading dispatches…</p>
        ) : shown === "unreadable" ? (
          <p className={s.note} data-testid="hq-dispatches-unreadable">
            The dispatches can&apos;t be read right now. Notifications are
            unaffected.
          </p>
        ) : shown === "failed" ? (
          <p className={s.note} data-testid="hq-dispatches-failed">
            Couldn&apos;t read the dispatches.{" "}
            <button
              type="button"
              className={s.backLink}
              onClick={() => setRetry((n) => n + 1)}
            >
              Retry
            </button>
          </p>
        ) : ordered.length === 0 ? (
          <p className={s.empty} data-testid="hq-dispatches-empty">
            No dispatches yet. HQ writes one here when something needs you.
          </p>
        ) : (
          <div className={s.dBody}>
            {ordered.map((d, i) => {
              const sep = seps[i];
              const isLinked = linked?.id === d.id;
              const n = d.members.length;
              return (
                <div key={d.id} className={s.dItem}>
                  {sep ? <div className={s.daysep}>{sep}</div> : null}
                  <div className={isLinked ? `nj-glow ${s.dGlow}` : undefined}>
                    <section
                      ref={isLinked ? linkedRef : undefined}
                      className={`nj-panel is-sm ${isLinked ? s.dspHit : ""}`}
                      aria-label={`Dispatch at ${clock(d.at)}`}
                      data-testid="hq-dispatch"
                      data-dispatch-id={d.id}
                      data-linked={isLinked ? "true" : undefined}
                    >
                      <div className={`nj-face ${s.dFace}`}>
                        <div className={`nj-callsign ${s.dCall}`}>
                          <span className="nj-notch" aria-hidden="true" />
                          <span className="nj-title">
                            {n > 1 ? "Bundle" : "Dispatch"}
                          </span>
                          <span aria-hidden="true">//</span>
                          <span className="nj-value">
                            {n > 1
                              ? `${n} events`
                              : look(d.members[0]?.class ?? "").word}
                          </span>
                          {isLinked ? (
                            <span
                              className="nj-stamp is-hud"
                              data-testid="hq-dispatch-linked"
                            >
                              Linked
                            </span>
                          ) : null}
                          <span className="nj-meta">{clock(d.at)}</span>
                        </div>
                        <ul className={s.members}>
                          {d.members.map((m, i) => (
                            <Member key={i} m={m} />
                          ))}
                        </ul>
                        <div className={s.dSrc}>{d.by} wrote</div>
                        <p className={s.hqline} data-testid="hq-dispatch-line">
                          {d.line}
                        </p>
                      </div>
                    </section>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className={s.readonly}>
        <p>
          HQ writes here when something needs you. To talk about a dispatch, ask
          in a conversation.
        </p>
        <Link
          className={s.newBtn}
          to={HQ_PATH}
          state={{ hqCompose: true }}
          data-testid="hq-dispatches-new"
        >
          <Plus size={16} aria-hidden="true" />
          New conversation
        </Link>
      </div>
    </section>
  );
}

function Member({ m }: { m: HqDispatchMember }) {
  const l = look(m.class);
  return (
    <li className={s.fixed} data-testid="hq-dispatch-member">
      <span className={`hud-led ${l.led} ${s.led}`} aria-hidden="true" />
      <span className={`${s.word} ${l.tone}`}>{l.word}</span>
      <span className={s.x}>{m.title}</span>
      {m.reason ? <span className={s.reason}>{m.reason}</span> : null}
      <span className={s.tag}>
        {l.kind} · {clock(m.ts)}
      </span>
      {m.link ? (
        <Link className={s.go} to={m.link} data-testid="hq-dispatch-open">
          {openLabel(m.link)}
        </Link>
      ) : null}
    </li>
  );
}
