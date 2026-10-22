// Framework-free core of the two ambient HUD effects (#211): the drifting data-flow backdrop
// and the occasional button glitch. Extracted from DataFlowCanvas/ButtonGlitch so the standalone
// Home Free connect shell (web/connect.html — plain DOM, no React until the SPA streams in) runs
// the *same* effects as the app instead of a look-alike copy.
//
// Both follow the ONE motion mode (#1531, theme/motion.ts): they run only while
// `ambientAllowed()` (Full, not paused by a hidden tab or 10 idle minutes) and, for the canvas,
// on the dark theme. A live switch is honoured without a reload: each subscribes to
// `onMotionChange` and tears its rAF loop / timers down, or starts them again. Each returns its
// own teardown; callers must invoke it (React does so from useEffect's cleanup).

import "./buttonGlitch.css";
import { ambientAllowed, onMotionChange } from "../../theme/motion";

const darkTheme = () => document.documentElement.dataset.theme !== "light";

/**
 * Paint the ambient data-flow field onto `canvas`: a faint 48px grid plus sparse signal trails
 * drifting rightward, radial-masked so it fades at the edges. Returns a teardown that stops the
 * rAF loop and unbinds the resize listener.
 */
/** Between two paints of the data-flow canvas (#1531): its trails drift a fraction of a pixel per
 *  frame, so a slower paint looks the same and the idle budget (e2e/motion-modes.spec.ts) holds. */
export const PAINT_MS = 50;

export function runDataFlow(canvas: HTMLCanvasElement): () => void {
  // `desynchronized`: the ambient canvas need not present in lock-step with the DOM, and skipping
  // that sync took its per-paint main-thread commit from ~0.9 ms to a fraction (#1531 idle budget:
  // 2.75% → 1.45% at the same 20 paints/s). Tearing is invisible on faint drifting trails.
  const ctx = canvas.getContext("2d", { desynchronized: true, alpha: true });
  if (!ctx) return () => {};

  let W = 0;
  let H = 0;
  let parts: {
    x: number;
    y: number;
    v: number;
    len: number;
    c: string;
    a: number;
  }[] = [];
  let raf = 0;
  let wait: ReturnType<typeof setTimeout> | undefined;
  let running = false;

  const mk = () => {
    const amber = Math.random() < 0.08;
    return {
      x: Math.random() * W,
      y: Math.random() * H,
      // Calmer than the mock default — matches the landing tuning the user asked for
      // ("too many and too fast"): slower drift + a lower density (see size()).
      v: 0.12 + Math.random() * 0.5,
      len: (30 + Math.random() * 90) * SCALE,
      c: amber ? "255,176,0" : "180,190,210",
      a: amber ? 0.8 : 0.32 + Math.random() * 0.3,
    };
  };
  // Half resolution: the canvas is stretched to the viewport by CSS (`width/height: 100%`), so it
  // paints a quarter of the pixels. The trails are faint and soft; the difference is invisible, and
  // a 2D canvas rasterises on the main thread wherever there is no GPU.
  const SCALE = 0.5;
  const size = () => {
    W = canvas.width = Math.max(1, Math.round(window.innerWidth * SCALE));
    H = canvas.height = Math.max(1, Math.round(window.innerHeight * SCALE));
    const mask =
      "radial-gradient(ellipse at center, #000 55%, transparent 95%)";
    canvas.style.maskImage = mask;
    canvas.style.webkitMaskImage = mask;
    const n = Math.min(26, Math.round((window.innerWidth * window.innerHeight) / 52000));
    parts = Array.from({ length: n }, mk);
  };
  /** The still frame Reduced / Off / paused leave behind: empty — the 48 px grid itself is CSS
   *  behind the canvas (`.hud-canvas` background), so it never costs a frame. */
  const still = () => {
    ctx.clearRect(0, 0, W, H);
  };
  // ~20 fps: the trails drift a fraction of a pixel per frame, so painting every other frame looks
  // the same. Each frame fades what is there toward transparent (one composite fill, no full-screen
  // repaint of a grid) and strokes each trail once in a flat colour: no per-particle gradient
  // objects. Together that keeps the idle budget in e2e/motion-modes.spec.ts.
  //
  // The wait between paints is a TIMER, not skipped animation frames: every frame the page asks for
  // is a main-thread frame (a style pass for the ambient CSS layers included), so a loop that asked
  // every vsync and painted one in three cost more the faster the compositor got. Asking only when
  // it will paint caps the page's ambient frames at ~20/s on any display (#1531 idle budget).
  let lastPaint = 0;
  const later = (ms: number) => {
    wait = setTimeout(() => {
      wait = undefined;
      if (running) raf = requestAnimationFrame(frame);
    }, ms);
  };
  const halt = () => {
    cancelAnimationFrame(raf);
    if (wait !== undefined) clearTimeout(wait);
    wait = undefined;
  };
  const frame = (now: number = performance.now()) => {
    if (!running) return;
    if (now - lastPaint < PAINT_MS) {
      later(PAINT_MS - (now - lastPaint));
      return;
    }
    const steps = lastPaint ? Math.min(4, (now - lastPaint) / 16.7) * SCALE : SCALE;
    lastPaint = now;
    ctx.globalCompositeOperation = "destination-out";
    ctx.fillStyle = "rgba(0,0,0,0.45)";
    ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = 1;
    for (const p of parts) {
      ctx.strokeStyle = `rgba(${p.c},${p.a * 0.6})`;
      ctx.beginPath();
      ctx.moveTo(p.x - p.len * 0.35, p.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      ctx.fillStyle = `rgba(${p.c},${Math.min(1, p.a + 0.3)})`;
      ctx.fillRect(p.x - 0.5, p.y - 0.5, 1.4, 1.4);
      p.x += p.v * steps;
      if (p.x - p.len > W) {
        p.x = -Math.random() * 80;
        p.y = Math.random() * H;
      }
    }
    later(PAINT_MS);
  };
  const onResize = () => {
    size();
    if (!running) still();
  };
  /** Start or stop the loop to match the mode, the pause and the theme. Idempotent. */
  const sync = () => {
    const want = ambientAllowed() && darkTheme();
    if (want === running) return;
    running = want;
    if (want) {
      lastPaint = 0;
      raf = requestAnimationFrame(frame);
    }
    else {
      halt();
      still();
    }
  };

  size();
  still();
  sync();
  window.addEventListener("resize", onResize);
  const unsubscribe = onMotionChange(sync);
  return () => {
    running = false;
    halt();
    unsubscribe();
    window.removeEventListener("resize", onResize);
  };
}

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
