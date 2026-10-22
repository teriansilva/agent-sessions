/** Whether the boot sequence runs now (#1531, docs/design.md v2 §8 rule 5): once per visit (per
 *  tab, `sessionStorage`), Full motion only, never under automation; any input or a switch away from
 *  Full ends it. See BootSequence.tsx. */
import { useEffect, useState } from "react";

import { useMotion } from "../../theme/motionStore";

export const BOOT_KEY = "bl-booted";
export const BOOT_MS = 1200;

function alreadyBooted(): boolean {
  // Automation (Playwright, the promo captures, the NIGHTJAR sweep) sees the settled app: a boot
  // layer over every first frame would only blur screenshots. e2e/boot-sequence.spec.ts clears
  // the flag to test the boot itself.
  if (typeof navigator !== "undefined" && navigator.webdriver) return true;
  try {
    return sessionStorage.getItem(BOOT_KEY) === "1";
  } catch {
    return true; // no storage: never risk a boot on every load
  }
}

export function useBoot(): boolean {
  const { mode } = useMotion();
  const [on, setOn] = useState(() => mode === "full" && !alreadyBooted());
  useEffect(() => {
    if (!on) return;
    try {
      sessionStorage.setItem(BOOT_KEY, "1");
    } catch {
      /* storage blocked */
    }
    const end = () => setOn(false);
    const t = setTimeout(end, BOOT_MS);
    const events = ["keydown", "pointerdown", "wheel", "touchstart"] as const;
    for (const e of events) window.addEventListener(e, end, { capture: true, passive: true });
    return () => {
      clearTimeout(t);
      for (const e of events) window.removeEventListener(e, end, { capture: true });
    };
  }, [on]);
  // A switch away from Full mid-boot ends it.
  if (on && mode !== "full") setOn(false);
  return on;
}
