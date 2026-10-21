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
