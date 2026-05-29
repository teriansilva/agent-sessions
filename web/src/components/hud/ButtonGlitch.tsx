import { useEffect } from "react";

// Occasional ambient button glitch (#211 Phase 4a) — the user-requested replacement for the
// old gold shine sweep. Every ~7–23s it briefly adds `.glitching` to a random visible
// `.shine` CTA, which runs the cosmetic clip/translate keyframes (App.css). Never disables
// the button (no pointer-events change) and is a no-op under prefers-reduced-motion. Renders
// nothing; mounted once at the app root.
export function ButtonGlitch() {
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    let timer: ReturnType<typeof setTimeout>;
    let clearCls: ReturnType<typeof setTimeout>;
    const schedule = () => {
      timer = setTimeout(
        () => {
          const btns = Array.from(document.querySelectorAll<HTMLElement>(".shine")).filter(
            (b) => b.offsetParent !== null, // visible only
          );
          if (btns.length) {
            const b = btns[Math.floor(Math.random() * btns.length)];
            b.classList.add("glitching");
            clearCls = setTimeout(() => b.classList.remove("glitching"), 300);
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
    };
  }, []);
  return null;
}
