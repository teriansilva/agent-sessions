import { useEffect, useRef } from "react";

// Ambient tactical-HUD "data-flow" backdrop (#211 Phase 4): faint drifting signal trails +
// a subtle grid behind the whole app, radial-masked so it fades at the edges. Mirrors the
// landing canvas + the family design language (the shared design system). Honors
// prefers-reduced-motion (renders nothing animated) and is hidden on the light theme via CSS
// (a static grid takes over there — see index.css), so this only animates on dark.
//
// Deliberately cosmetic + non-interactive (pointer-events:none, behind z-index:0). All state
// is local to the effect and torn down on unmount; the rAF loop stops via a cancelled flag.
export function DataFlowCanvas() {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return; // static-only; CSS still paints the faint grid on light/reduced
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let W = 0;
    let H = 0;
    let parts: { x: number; y: number; v: number; len: number; c: string; a: number }[] = [];
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
      const mask = "radial-gradient(ellipse at center, #000 55%, transparent 95%)";
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
  }, []);

  return <canvas id="bg" className="hud-canvas" ref={ref} aria-hidden="true" />;
}
