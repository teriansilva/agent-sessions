import { useEffect, useRef } from "react";

import { runDataFlow } from "./dataFlow";

// Ambient tactical-HUD "data-flow" backdrop (#211 Phase 4): faint drifting signal trails +
// a subtle grid behind the whole app, radial-masked so it fades at the edges. Mirrors the
// landing canvas + the family design language (the shared design system). Honors
// prefers-reduced-motion (renders nothing animated) and is hidden on the light theme via CSS
// (a static grid takes over there — see index.css), so this only animates on dark.
//
// Deliberately cosmetic + non-interactive (pointer-events:none, behind z-index:0). The effect
// itself lives in dataFlow.ts so the standalone connect shell runs the same backdrop.
export function DataFlowCanvas() {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    return runDataFlow(canvas);
  }, []);

  return <canvas id="bg" className="hud-canvas" ref={ref} aria-hidden="true" />;
}
