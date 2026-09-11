// Framework-free core of the two ambient HUD effects (#211): the drifting data-flow backdrop
// and the occasional button glitch. Extracted from DataFlowCanvas/ButtonGlitch so the standalone
// Home Free connect shell (web/connect.html — plain DOM, no React until the SPA streams in) runs
// the *same* effects as the app instead of a look-alike copy.
//
// Both are cosmetic, non-interactive, and a no-op under prefers-reduced-motion. Each returns its
// own teardown; callers must invoke it (React does so from useEffect's cleanup).

import "./buttonGlitch.css";

const noop = () => {};

function prefersReducedMotion(): boolean {
  return (
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false
  );
}

/**
 * Paint the ambient data-flow field onto `canvas`: a faint 48px grid plus sparse signal trails
 * drifting rightward, radial-masked so it fades at the edges. Returns a teardown that stops the
 * rAF loop and unbinds the resize listener.
 */
export function runDataFlow(canvas: HTMLCanvasElement): () => void {
  if (prefersReducedMotion()) return noop; // static-only; CSS paints the faint grid instead
  const ctx = canvas.getContext("2d");
  if (!ctx) return noop;

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
  let cancelled = false;

  const mk = () => {
    const amber = Math.random() < 0.08;
    return {
      x: Math.random() * W,
      y: Math.random() * H,
      // Calmer than the mock default — matches the landing tuning the user asked for
      // ("too many and too fast"): slower drift + a lower density (see size()).
      v: 0.12 + Math.random() * 0.5,
      len: 30 + Math.random() * 90,
      c: amber ? "255,176,0" : "180,190,210",
      a: amber ? 0.8 : 0.32 + Math.random() * 0.3,
    };
  };
  const size = () => {
    W = canvas.width = window.innerWidth;
    H = canvas.height = window.innerHeight;
    const mask =
      "radial-gradient(ellipse at center, #000 55%, transparent 95%)";
    canvas.style.maskImage = mask;
    canvas.style.webkitMaskImage = mask;
    const n = Math.min(34, Math.round((W * H) / 44000));
    parts = Array.from({ length: n }, mk);
  };
  const grid = () => {
    // A touch more present than a hairline (#211 review): the floating panels are frosted
    // glass, and a continuous grid is what makes the backdrop-blur actually read — sparse
    // trails alone are too thin to show a blur. Still faint enough to stay ambient.
    ctx.strokeStyle = "rgba(150,170,210,0.06)";
    ctx.lineWidth = 1;
    for (let x = 0; x < W; x += 48) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
      ctx.stroke();
    }
    for (let y = 0; y < H; y += 48) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
    }
  };
  const frame = () => {
    if (cancelled) return;
    ctx.fillStyle = "rgba(13,14,16,0.55)";
    ctx.fillRect(0, 0, W, H);
    grid();
    for (const p of parts) {
      const g = ctx.createLinearGradient(p.x - p.len, p.y, p.x, p.y);
      g.addColorStop(0, `rgba(${p.c},0)`);
      g.addColorStop(1, `rgba(${p.c},${p.a})`);
      ctx.strokeStyle = g;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(p.x - p.len, p.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      ctx.fillStyle = `rgba(${p.c},${Math.min(1, p.a + 0.3)})`;
      ctx.fillRect(p.x - 1, p.y - 1, 2.2, 2.2);
      p.x += p.v;
      if (p.x - p.len > W) {
        p.x = -Math.random() * 80;
        p.y = Math.random() * H;
      }
    }
    raf = requestAnimationFrame(frame);
  };

  size();
  frame();
  window.addEventListener("resize", size);
  return () => {
    cancelled = true;
    cancelAnimationFrame(raf);
    window.removeEventListener("resize", size);
  };
}

/**
 * Every ~7–23s, briefly glitch one visible, enabled button control (including section/gear
 * links). The SPA and connect shell share geometry-stable feedback in buttonGlitch.css.
 * Reduced motion, hidden/inert controls and disabled actions stay quiet. Teardown clears
 * both timers and any in-flight class before the connect shell yields to the SPA.
 */
export function runButtonGlitch(): () => void {
  if (prefersReducedMotion()) return noop;
  let timer: ReturnType<typeof setTimeout>;
  let clearCls: ReturnType<typeof setTimeout>;
  let current: HTMLElement | undefined;
  const schedule = () => {
    timer = setTimeout(
      () => {
        const btns = prefersReducedMotion()
          ? []
          : Array.from(
              document.querySelectorAll<HTMLElement>(
                'button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"], .shine, .gear, .section-nav a',
              ),
            ).filter((b) => {
              if (
                b.matches(":disabled") ||
                b.closest(
                  '[inert], [hidden], [aria-hidden="true"], [aria-disabled="true"]',
                )
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
        if (btns.length) {
          const b = btns[Math.floor(Math.random() * btns.length)];
          current = b;
          b.classList.add("glitching");
          clearCls = setTimeout(() => {
            b.classList.remove("glitching");
            current = undefined;
          }, 300);
        }
        schedule();
      },
      7000 + Math.random() * 16000,
    );
  };
  schedule();
  return () => {
    clearTimeout(timer);
    clearTimeout(clearCls);
    current?.classList.remove("glitching");
  };
}
