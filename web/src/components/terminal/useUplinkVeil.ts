import { useEffect, useRef, useState } from "react";
import type { TermStatus } from "../../lib/termSocket";

/** The uplink veil's clock (#1570). A connect that has not opened within CONNECT_GRACE_MS, or a link
 *  that has been down for LOST_GRACE_MS, is announced over the terminal; anything shorter shows
 *  nothing new, so a blip never flickers the pane. Once shown the veil stays MIN_SHOWN_MS, says
 *  RESTORED for RESTORED_HOLD_MS, then fades out over LEAVE_MS (--t-slow in Full). A tab coming back
 *  into view gets VISIBLE_GRACE_MS first: the socket's own wake retries at once, and the veil must
 *  not flash while it does. A hidden tab never mounts the veil at all: an outage that passes its
 *  grace in the background is held, and on return it gets the same VISIBLE_GRACE_MS before it shows
 *  (or never shows, if the wake reconnects in time).
 *
 *  A link only counts as back once it has held for STABLE_MS. A socket that opens and is closed at
 *  once (a busy or restarting server that accepts and then refuses) is part of the same outage: it
 *  neither restarts the outage clock nor ends an announcement, so a flapping link still shows the
 *  veil instead of resetting it on every retry. */
export const CONNECT_GRACE_MS = 3000;
export const LOST_GRACE_MS = 5000;
export const MIN_SHOWN_MS = 600;
export const RESTORED_HOLD_MS = 600;
export const LEAVE_MS = 320;
export const VISIBLE_GRACE_MS = 1000;
export const STABLE_MS = 750;

export type VeilPhase = "establishing" | "lost" | "restored";

export interface UplinkVeil {
  phase: VeilPhase;
  /** When the outage began (ms epoch): the mount for a first connect, the drop for a lost link. */
  since: number;
  /** The connection try in progress within this outage, counted from 1. Counted here, not read off
   *  the socket: `TermSocket` resets its backoff counter on every open, so a flapping link would
   *  read "1" forever. */
  attempt: number;
  /** RESTORED only: how long the link was down. */
  downMs?: number;
  /** The fade-out is running; the veil unmounts LEAVE_MS later. */
  leaving: boolean;
}

/** Drive the veil from the socket status. One instance per `<Terminal>` mount, and a session switch
 *  remounts the pane, so "never connected on this mount" is exactly "opening or switching to it". */
export function useUplinkVeil(status: TermStatus): UplinkVeil | null {
  const [veil, setVeil] = useState<UplinkVeil | null>(null);
  const live = useRef({
    veil: null as UplinkVeil | null,
    everConnected: false,
    connectedAt: 0,
    tries: 1,
    prevKind: null as TermStatus["kind"] | null,
    downSince: 0, // the mount time, stamped by the first effect below
    shownAt: 0,
    visibleAt: 0,
    timer: null as ReturnType<typeof setTimeout> | null,
    /** An outage whose grace ran out while the tab was hidden, waiting for the tab to return. */
    held: null as (() => void) | null,
  });

  useEffect(() => {
    const st = live.current;
    st.downSince = Date.now(); // a first connect's outage clock starts at the mount
    const onVisible = () => {
      if (document.visibilityState === "hidden") {
        // An outage already on screen comes down while the tab is hidden and is held — its clock and
        // try count kept — so the return applies the same 1 s grace as an outage that began hidden.
        const v = st.veil;
        if (v && v.phase !== "restored") {
          if (st.timer !== null) clearTimeout(st.timer);
          st.timer = null;
          st.veil = null;
          setVeil(null);
          const reveal = () => {
            // The rule is checked HERE, at the moment of showing, not by whoever armed the timer:
            // never behind a hidden tab, and never within VISIBLE_GRACE_MS of the LATEST return. A
            // stale timer from an earlier return (the tab hid and came back again meanwhile) only
            // reschedules itself.
            if (document.visibilityState === "hidden") {
              st.held = reveal;
              return;
            }
            const wait = st.visibleAt + VISIBLE_GRACE_MS - Date.now();
            if (wait > 0) {
              if (st.timer !== null) clearTimeout(st.timer);
              st.timer = setTimeout(reveal, wait);
              return;
            }
            st.shownAt = Date.now();
            st.veil = { ...v, attempt: st.tries, leaving: false };
            setVeil(st.veil);
          };
          st.held = reveal;
        }
        return;
      }
      if (document.visibilityState !== "visible") return;
      st.visibleAt = Date.now();
      // A held outage gets its return grace now; a status change before then re-decides it.
      const held = st.held;
      st.held = null;
      if (held) {
        if (st.timer !== null) clearTimeout(st.timer);
        st.timer = setTimeout(held, VISIBLE_GRACE_MS);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (st.timer !== null) clearTimeout(st.timer);
    };
  }, []);

  const attempt = status.kind === "reconnecting" ? status.attempt : 0;

  useEffect(() => {
    const st = live.current;
    const publish = (v: UplinkVeil | null) => {
      st.veil = v;
      setVeil(v);
    };
    const later = (fn: () => void, ms: number) => {
      st.timer = setTimeout(fn, Math.max(0, ms));
    };
    if (st.timer !== null) clearTimeout(st.timer);
    st.timer = null;
    st.held = null;
    const prev = st.prevKind;
    st.prevKind = status.kind;
    const cur = st.veil;

    // A deliberate reject and a wrong runtime have their own panels; the veil never covers them.
    if (status.kind === "rejected" || status.kind === "wrong-runtime") {
      publish(null);
      return;
    }

    if (status.kind === "connected") {
      st.connectedAt = Date.now();
      if (!cur || cur.phase === "restored") {
        if (cur) publish(null); // reconnected again mid-fade: just finish
        return;
      }
      // Hold the announcement until the link has held STABLE_MS and the veil has been up its
      // minimum, then RESTORED, then the fade, then gone. A drop in between cancels this timer.
      const since = st.downSince;
      const restore = () => {
        publish({ phase: "restored", since, attempt: cur.attempt, downMs: Date.now() - since, leaving: false });
        later(() => {
          publish({ ...(st.veil as UplinkVeil), leaving: true });
          later(() => publish(null), LEAVE_MS);
        }, RESTORED_HOLD_MS);
      };
      later(restore, Math.max(STABLE_MS, MIN_SHOWN_MS - (Date.now() - st.shownAt)));
      return;
    }

    // connecting / reconnecting. A drop from a live link starts a new outage clock; retries inside
    // one outage keep it.
    if (prev === "connected") {
      const held = Date.now() - st.connectedAt;
      if (held >= STABLE_MS) {
        // A real drop from a link that had held: a new outage, on its own clock and try count.
        st.everConnected = true;
        st.downSince = Date.now();
        st.tries = 1;
        if (cur?.phase === "restored") publish(null); // dropped again while RESTORED showed
      } else {
        st.tries += 1; // it flapped (opened and closed at once): the same outage, the next try
      }
    } else if (prev !== null) {
      st.tries += 1; // the socket moved on to its next retry
    }
    const lost = st.everConnected;
    const phase: VeilPhase = lost ? "lost" : "establishing";
    const tryNo = st.tries;
    if (st.veil) {
      publish({ ...st.veil, phase, attempt: tryNo }); // already announced: refresh the count
      return;
    }
    const grace = lost ? LOST_GRACE_MS : CONNECT_GRACE_MS;
    const show = () => {
      if (document.visibilityState === "hidden") {
        st.held = show; // never mount behind a hidden tab; the return decides
        return;
      }
      const now = Date.now();
      const wait = Math.max(st.downSince + grace - now, st.visibleAt + VISIBLE_GRACE_MS - now);
      if (wait > 0) {
        later(show, wait);
        return;
      }
      st.shownAt = now;
      publish({ phase, since: st.downSince, attempt: tryNo, leaving: false });
    };
    show();
  }, [status.kind, attempt]);

  return veil;
}
