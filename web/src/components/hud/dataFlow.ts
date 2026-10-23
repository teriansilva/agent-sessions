// Framework-free core of the occasional button glitch (#211, #946). The app runs it through
// ButtonGlitch.tsx; the standalone Home Free connect page (web/connect.html, plain DOM) calls it
// directly and tears it down when the streamed app takes the screen. The drifting data-flow
// canvas that used to live here is retired: the app's backdrop is NIGHTJAR's atmosphere
// (Atmosphere.tsx, #1531), and the connect page wears the same `.nj-atmo` markup since #1596.
//
// It follows the ONE motion mode (#1531, theme/motion.ts): it runs only while `ambientAllowed()`
// (Full, not paused by a hidden tab or 10 idle minutes), subscribes to `onMotionChange` so a live
// switch is honoured without a reload, and returns its own teardown.

import "./buttonGlitch.css";
import { ambientAllowed, onMotionChange } from "../../theme/motion";

/**
 * Every ~7–23s, briefly glitch one visible, enabled button control (including section/gear
 * links). The SPA and connect shell share geometry-stable feedback in buttonGlitch.css.
 * Reduced motion, hidden/inert controls and disabled actions stay quiet. Teardown clears
 * both timers and any in-flight class before the connect shell yields to the SPA.
 */
export function runButtonGlitch(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let clearCls: ReturnType<typeof setTimeout> | undefined;
  let current: HTMLElement | undefined;
  const pick = () => {
    const btns = Array.from(
      document.querySelectorAll<HTMLElement>(
        'button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"], .shine, .gear, .section-nav a',
      ),
    ).filter((b) => {
      if (
        b.matches(":disabled") ||
        b.closest('[inert], [hidden], [aria-hidden="true"], [aria-disabled="true"]')
      )
        return false;
      const style = getComputedStyle(b);
      return (
        (b.offsetParent !== null || b.getClientRects().length > 0) &&
        style.visibility !== "hidden" &&
        style.visibility !== "collapse" &&
        style.opacity !== "0"
      );
    });
    if (!btns.length) return;
    const b = btns[Math.floor(Math.random() * btns.length)];
    current = b;
    b.classList.add("glitching");
    clearCls = setTimeout(() => {
      b.classList.remove("glitching");
      current = undefined;
    }, 300);
  };
  const schedule = () => {
    timer = setTimeout(
      () => {
        timer = undefined;
        if (!ambientAllowed()) return; // the next sync() re-arms it
        pick();
        schedule();
      },
      7000 + Math.random() * 16000,
    );
  };
  const stop = () => {
    if (timer) clearTimeout(timer);
    if (clearCls) clearTimeout(clearCls);
    timer = clearCls = undefined;
    current?.classList.remove("glitching");
    current = undefined;
  };
  /** Armed only in Full and while nothing paused it; a live switch to Reduced/Off clears it. */
  const sync = () => {
    if (!ambientAllowed()) stop();
    else if (!timer) schedule();
  };
  sync();
  const unsubscribe = onMotionChange(sync);
  return () => {
    unsubscribe();
    stop();
  };
}
