// The motion mode (#1531, docs/design.md v2 §8) — framework-free, so the SPA, the standalone
// connect shell (connect.html, plain DOM until the SPA streams in) and the ambient effects in
// components/hud/dataFlow.ts all read ONE answer.
//
// - The CHOICE is what the operator picked in Settings → Appearance → Interface → Motion, stored
//   per device under `tr-motion` beside `tr-theme` / `tr-accent`. `system` (the default) follows
//   the device: `prefers-reduced-motion: reduce` → reduced, otherwise full.
// - The MODE is what is in force: `data-motion="full|reduced|off"` on <html>. It is applied
//   before first paint by the inline script in index.html (mirrors `resolveMotion` below), so no
//   animation can start before the choice is known. motion.css does the CSS side; JS loops ask
//   `ambientAllowed()` and subscribe with `onMotionChange()`.
// - The PAUSE is orthogonal: `data-motion-paused="hidden|idle"` while the tab is hidden or after
//   10 minutes without input. Ambient loops (CSS `.nj-ambient`, the canvas, the glitch timer, the
//   HQ portrait) stop; state and interaction motion is never paused, so nothing can freeze
//   half-faded.

export type MotionChoice = "system" | "full" | "reduced" | "off";
export type MotionMode = "full" | "reduced" | "off";
export type MotionPause = "hidden" | "idle";

export const MOTION_STORAGE_KEY = "tr-motion";
export const DEFAULT_MOTION_CHOICE: MotionChoice = "system";
export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
/** How long without input before the ambient loops stop (they resume on the next input). */
export const IDLE_PAUSE_MS = 10 * 60 * 1000;

const CHOICES: readonly MotionChoice[] = ["system", "full", "reduced", "off"];
const MODES: readonly MotionMode[] = ["full", "reduced", "off"];

export function isMotionChoice(v: unknown): v is MotionChoice {
  return typeof v === "string" && (CHOICES as readonly string[]).includes(v);
}

/** Never throws: anything unknown (a legacy or edited value) is the default. */
export function coerceMotionChoice(v: unknown): MotionChoice {
  return isMotionChoice(v) ? v : DEFAULT_MOTION_CHOICE;
}

export function resolveMotion(choice: MotionChoice, deviceReduced: boolean): MotionMode {
  if (choice === "system") return deviceReduced ? "reduced" : "full";
  return choice;
}

/** The device's answer. A browser without matchMedia (or a test environment) answers "no
 *  preference" — the BattleLab default is Full. */
export function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.(REDUCED_MOTION_QUERY).matches ?? false;
  } catch {
    return false;
  }
}

export function readStoredMotion(): MotionChoice {
  try {
    return coerceMotionChoice(localStorage.getItem(MOTION_STORAGE_KEY));
  } catch {
    return DEFAULT_MOTION_CHOICE;
  }
}

export function storeMotion(choice: MotionChoice): void {
  try {
    if (choice === DEFAULT_MOTION_CHOICE) localStorage.removeItem(MOTION_STORAGE_KEY);
    else localStorage.setItem(MOTION_STORAGE_KEY, choice);
  } catch {
    /* storage disabled — the attribute is still applied for this page */
  }
}

export function applyMotion(mode: MotionMode): void {
  document.documentElement.dataset.motion = mode;
}

/** The mode in force. Reads <html data-motion> (set pre-paint); a page that never set it (a test,
 *  a stripped inline script) resolves the stored choice against the device instead. */
export function motionMode(): MotionMode {
  const set = document.documentElement.dataset.motion;
  if (set && (MODES as readonly string[]).includes(set)) return set as MotionMode;
  return resolveMotion(readStoredMotion(), prefersReducedMotion());
}

export function motionPaused(): MotionPause | null {
  const p = document.documentElement.dataset.motionPaused;
  return p === "hidden" || p === "idle" ? p : null;
}

/** Ambient loops (atmosphere, glitch timer, portrait video, LED breathe) run only in Full and
 *  only while nothing paused them. */
export function ambientAllowed(): boolean {
  return motionMode() === "full" && motionPaused() === null;
}

/** Belt-and-suspenders boot from main.tsx, idempotent with the inline pre-paint script. */
export function bootMotion(): void {
  applyMotion(resolveMotion(readStoredMotion(), prefersReducedMotion()));
}

/** Call `cb` whenever the mode, the pause or the theme changes (all three gate ambient loops).
 *  One MutationObserver per subscriber, on <html>'s attributes only. Returns the unsubscribe. */
export function onMotionChange(cb: () => void): () => void {
  if (typeof MutationObserver === "undefined") return () => {};
  const mo = new MutationObserver(cb);
  mo.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-motion", "data-motion-paused", "data-theme"],
  });
  return () => mo.disconnect();
}

const INPUT_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"] as const;

/**
 * Own `data-motion-paused`: `hidden` while the document is hidden, `idle` after `idleMs` without
 * input, removed again on visibility / input. Returns the teardown. The SPA starts it from
 * MotionProvider; the connect shell starts its own.
 */
export function startMotionPause(idleMs: number = IDLE_PAUSE_MS): () => void {
  const root = document.documentElement;
  let idle = false;
  let last = Date.now();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const apply = () => {
    const next = document.visibilityState === "hidden" ? "hidden" : idle ? "idle" : null;
    if (next === null) delete root.dataset.motionPaused;
    else if (root.dataset.motionPaused !== next) root.dataset.motionPaused = next;
  };
  // One timer, re-armed for the REMAINING time when it fires early: input (pointermove is
  // frequent) only stamps a time, it never touches a timer or the DOM unless it ends an idle.
  const arm = (ms: number) => {
    timer = setTimeout(() => {
      const left = idleMs - (Date.now() - last);
      if (left > 0) return arm(left);
      idle = true;
      apply();
    }, ms);
  };
  const onInput = () => {
    last = Date.now();
    if (idle) {
      idle = false;
      apply();
      arm(idleMs);
    }
  };
  const onVisibility = () => {
    apply();
    if (document.visibilityState !== "hidden") onInput();
  };
  for (const e of INPUT_EVENTS) window.addEventListener(e, onInput, { passive: true, capture: true });
  document.addEventListener("visibilitychange", onVisibility);
  arm(idleMs);
  apply();
  return () => {
    if (timer) clearTimeout(timer);
    for (const e of INPUT_EVENTS) window.removeEventListener(e, onInput, { capture: true });
    document.removeEventListener("visibilitychange", onVisibility);
    delete root.dataset.motionPaused;
  };
}

/**
 * A page with no stored motion choice (the public share viewer, #1596): follow the device LIVE —
 * the mode tracks `prefers-reduced-motion` as it changes, not just at load — and own the hidden-tab
 * / idle pause, as MotionProvider does for the SPA. Returns the teardown.
 */
export function followDeviceMotion(idleMs: number = IDLE_PAUSE_MS): () => void {
  let mq: MediaQueryList | undefined;
  try {
    mq = window.matchMedia?.(REDUCED_MOTION_QUERY);
  } catch {
    mq = undefined;
  }
  const apply = () => applyMotion(resolveMotion("system", mq?.matches ?? false));
  apply();
  mq?.addEventListener?.("change", apply);
  const stopPause = startMotionPause(idleMs);
  return () => {
    mq?.removeEventListener?.("change", apply);
    stopPause();
  };
}
