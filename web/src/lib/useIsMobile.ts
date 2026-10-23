import { useEffect, useState } from "react";

/** The app's ONE mobile predicate (≤800px). Extracted from App.tsx (#208) so the window
 *  workspace gates on the same breakpoint the shell does rather than inventing a second one —
 *  two predicates would drift, and the layout they describe is the same layout. */
export const MOBILE_QUERY = "(max-width: 800px)";

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia?.(MOBILE_QUERY).matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.(MOBILE_QUERY);
    if (!mq) return;
    const on = () => setIsMobile(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return isMobile;
}

/** The session head's compact form (#1589): the ≤800px phone line, OR a touch screen up to 1180px —
 *  a tablet (an iPad, a car's browser) gets the desktop layout and the 44px touch targets, and a
 *  head of 44px icon squares left its facts reading "o…". There it folds its actions into the same
 *  single Actions menu a phone uses. Mirrored in Terminal.module.css (the trigger's 44px styling). */
export const COMPACT_HEAD_QUERY = "(max-width: 800px), (pointer: coarse) and (max-width: 1180px)";

export function useCompactHead(): boolean {
  const [compact, setCompact] = useState(
    () => window.matchMedia?.(COMPACT_HEAD_QUERY).matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.(COMPACT_HEAD_QUERY);
    if (!mq) return;
    const on = () => setCompact(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return compact;
}
