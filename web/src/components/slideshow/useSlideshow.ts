import { useCallback, useEffect, useState } from "react";

/** One slideshow for the setup tour and What's new (#971): the position, Back / Next and ←/→.
 *  Each consumer renders its own slide and buttons, because the tour sits in the onboarding card
 *  and What's new on the HUD dialog sheet; `SlideDots` and `SlideCounter` render the position. */

export interface SlideshowState {
  index: number;
  count: number;
  first: boolean;
  last: boolean;
  next: () => void;
  back: () => void;
  goTo: (index: number) => void;
}

export function useSlideshow(
  count: number,
  { keyboard = true }: { keyboard?: boolean } = {},
): SlideshowState {
  const [index, setIndex] = useState(0);
  const max = Math.max(0, count - 1);
  const next = useCallback(() => setIndex((i) => Math.min(max, i + 1)), [max]);
  const back = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);
  const goTo = useCallback((i: number) => setIndex(Math.max(0, Math.min(max, i))), [max]);

  useEffect(() => {
    if (!keyboard) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target;
      if (
        t instanceof HTMLElement &&
        (t.isContentEditable || t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")
      )
        return;
      if (e.key === "ArrowRight") {
        e.preventDefault();
        next();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        back();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [keyboard, next, back]);

  const safe = Math.min(index, max);
  return { index: safe, count, first: safe === 0, last: safe >= max, next, back, goTo };
}
