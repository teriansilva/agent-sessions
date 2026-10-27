/** The API transcript's follow-scroll (#1379, #1727).
 *
 *  Each session starts pinned to the bottom. Scroll geometry within 4 px of the bottom resumes
 *  following; scrolling up stops it and the reading position is never moved again until the
 *  reader comes back (or presses Jump to latest). While following, growth — a streamed reply, a
 *  new row, a delayed image, a resize — is followed:
 *
 *  - in Full motion the scroll EASES to the bottom on the shared frame scheduler
 *    (`theme/frames.ts`), retargeting every frame as the content keeps growing, and stops the
 *    frame it arrives (no loop while idle);
 *  - in Reduced and Off it lands at once (Reduced moves nothing but ≤ 120 ms fades, design.md §8);
 *  - the first paint of a session, and any jump longer than 1.5 views (a reconnect's catch-up),
 *    land at once too: the reader is not shown a scroll through history.
 *
 *  Growth (the content's or the box's height) is followed, and so is a view knocked more than 4 px
 *  off the bottom (anchoring after a window slide); a mutation that changes neither is not. The scroll events the ease itself causes are recognised (`ours`) and never read as
 *  the reader scrolling. While the reader is away, new rows (turns, tool rows, requests — never text flushes)
 *  are counted and growth below the view is noted: that is what Jump to latest offers. */
import { type RefObject, useCallback, useLayoutEffect, useRef, useState } from "react";

import { scheduleFrames } from "../../theme/frames";
import { motionMode } from "../../theme/motion";

/** The ease's time constant: ~95% of the way in 3τ. */
const TAU_MS = 70;
/** A jump longer than this many views lands at once. */
const EASE_MAX_VIEWS = 1.5;

export interface Follow {
  /** The reader is above the bottom (not following). */
  away: boolean;
  /** New rows that arrived while away. */
  rows: number;
  /** Something grew below the view while away (a reply streaming on, a row). */
  grew: boolean;
  /** Back to the bottom, eased like any follow, and following again. */
  jump: () => void;
  /** `count` new rows arrived (counted only while away). */
  noteRows: (count: number) => void;
}

export function useFollow(
  logRef: RefObject<HTMLElement | null>,
  /** The session (and anything that remounts the log): each starts pinned, at once. */
  key: unknown,
  remount: unknown,
  /** The pane has shown a snapshot: until then every follow lands at once (first paint). */
  shown: boolean,
): Follow {
  const [away, setAway] = useState(false);
  const [news, setNews] = useState({ rows: 0, grew: false });
  const controls = useRef<{ jump: () => void; noteRows: (count: number) => void }>({
    jump: () => {},
    noteRows: () => {},
  });
  const shownRef = useRef(shown);
  // Before the follow effect's observers run (they fire after this commit's layout effects).
  useLayoutEffect(() => {
    shownRef.current = shown;
  }, [shown]);

  useLayoutEffect(() => {
    const el = logRef.current;
    if (!el) return;
    let following = true;
    let primed = false; // the first paint with a snapshot has landed
    let lastTop = el.scrollTop;
    let lastBelow = el.scrollHeight - el.scrollTop;
    let ours: number | null = null; // where our own last write put the scroll
    let easing: (() => void) | null = null;
    let lastGeometry = "";
    setAway(false);
    setNews((n) => (n.rows || n.grew ? { rows: 0, grew: false } : n));

    const bottom = () => Math.max(0, el.scrollHeight - el.clientHeight);
    const write = (top: number) => {
      el.scrollTop = top;
      ours = el.scrollTop;
      lastTop = el.scrollTop;
      lastBelow = el.scrollHeight - el.scrollTop;
    };
    const stop = () => {
      easing?.();
      easing = null;
    };
    const setFollowing = (next: boolean) => {
      if (following === next) return;
      following = next;
      setAway(!next);
      if (next) setNews({ rows: 0, grew: false });
      else stop();
    };
    const settle = () => {
      const target = bottom();
      if (
        !primed ||
        motionMode() !== "full" ||
        target - el.scrollTop > el.clientHeight * EASE_MAX_VIEWS
      ) {
        stop();
        write(target);
        if (shownRef.current) primed = true;
        return;
      }
      if (easing || target - el.scrollTop <= 1) {
        if (!easing) write(target);
        return; // a running ease reads the new target on its next frame
      }
      let last = performance.now();
      easing = scheduleFrames((now) => {
        if (!following) return false;
        const goal = bottom();
        const pos = el.scrollTop;
        const left = goal - pos;
        const dt = Math.min(64, Math.max(0, now - last));
        last = now;
        if (left <= 1) {
          write(goal);
          easing = null;
          return false;
        }
        write(pos + Math.max(1, left * (1 - Math.exp(-dt / TAU_MS))));
        if (el.scrollTop <= pos) {
          // The box could not move (clamped): it is as far as it goes.
          easing = null;
          return false;
        }
        return true;
      });
    };
    const follow = () => {
      // Growth is followed — the content's or the box's height — and so is a view that ended up
      // more than 4 px off the bottom: a snapshot window that slides (its oldest turn out, a
      // same-height one in) changes no height, yet scroll anchoring moves the view up by the
      // removed turn (review of #1747). A mutation that changes neither (a live reply dropping
      // its faded-word spans, #1727) must not pull a reader dragging up inside the band back down.
      const geometry = `${el.scrollHeight}:${el.clientHeight}`;
      const changed = geometry !== lastGeometry;
      lastGeometry = geometry;
      if (following) {
        if (changed || !primed || bottom() - el.scrollTop > 4) settle();
        return;
      }
      // Growth BELOW the view (a prepend above keeps this distance: scroll anchoring moves both).
      const below = el.scrollHeight - el.scrollTop;
      if (below > lastBelow + 1) setNews((n) => (n.grew ? n : { ...n, grew: true }));
      lastBelow = below;
    };
    const onScroll = () => {
      const top = el.scrollTop;
      lastBelow = el.scrollHeight - top;
      if (ours !== null && top === ours) {
        lastTop = top; // the ease's own scroll, not the reader's
        return;
      }
      ours = null;
      // The reader moved up: an ease still finishing its last pixels yields at once, never
      // fighting a slow drag inside the 4 px band.
      if (top < lastTop) stop();
      if (el.scrollHeight - el.clientHeight - top <= 4) setFollowing(true);
      else if (top < lastTop) setFollowing(false);
      // A delayed event from our own scroll can arrive after content grows again. An unchanged
      // position is not the operator scrolling up, so it must not turn following off.
      lastTop = top;
    };
    controls.current = {
      jump: () => {
        setFollowing(true);
        settle();
      },
      noteRows: (count: number) => {
        if (!following && count > 0) setNews((n) => ({ ...n, rows: n.rows + count }));
      },
    };
    // Observe the transcript's children too: the scroll box keeps its height while a streamed
    // reply or a delayed image grows. Browser anchoring preserves reading above the bottom.
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(follow);
    const observe = () => {
      resize?.disconnect();
      resize?.observe(el, { box: "border-box" });
      for (const child of el.children) resize?.observe(child, { box: "border-box" });
      follow();
    };
    const changes = new MutationObserver(observe);
    changes.observe(el, { childList: true, characterData: true, subtree: true });
    el.addEventListener("scroll", onScroll, { passive: true });
    observe();
    return () => {
      stop();
      el.removeEventListener("scroll", onScroll);
      changes.disconnect();
      resize?.disconnect();
      controls.current = { jump: () => {}, noteRows: () => {} };
    };
  }, [logRef, key, remount]);

  const jump = useCallback(() => controls.current.jump(), []);
  const noteRows = useCallback((count: number) => controls.current.noteRows(count), []);
  return { away, rows: news.rows, grew: news.grew, jump, noteRows };
}
