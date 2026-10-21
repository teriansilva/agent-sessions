import { useEffect, useRef } from "react";

import { runDataFlow } from "./dataFlow";

// The ambient layer behind the whole app (#211 Phase 4, NIGHTJAR atmosphere #1531): the drifting
// data-flow canvas plus the atmosphere's text-free layers — hex grid, scanlines, noise grain, the
// sweep band and the vignette (nightjar.css `.nj-atmo`). Everything follows the ONE motion mode
// (theme/motion.ts): Full animates on dark; Reduced, Off, a hidden tab or 10 idle minutes leave a
// still frame. Light shows the static hex grid only.
//
// Deliberately cosmetic and non-interactive (pointer-events: none, behind everything). The canvas
// effect itself lives in dataFlow.ts so the standalone connect shell runs the same backdrop.
export function DataFlowCanvas() {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    return runDataFlow(canvas);
  }, []);

  return (
    <>
      <canvas id="bg" className="hud-canvas" ref={ref} aria-hidden="true" />
      <div className="nj-atmo" aria-hidden="true" data-testid="nj-atmo">
        <i className="nj-hex" />
        <i className="nj-scan" />
        <i className="nj-noise" />
        <i className="nj-sweep" />
        <i className="nj-vig" />
      </div>
    </>
  );
}
