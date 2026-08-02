import { useEffect } from "react";

import { runButtonGlitch } from "./dataFlow";

// Occasional ambient button glitch (#211 Phase 4a) — the user-requested replacement for the
// old gold shine sweep. Every ~7–23s it briefly adds `.glitching` to a random visible
// `.shine` CTA, which runs the cosmetic clip/translate keyframes (App.css). Never disables
// the button (no pointer-events change) and is a no-op under prefers-reduced-motion. Renders
// nothing; mounted once at the app root. The timer lives in dataFlow.ts, shared with the
// standalone connect shell.
export function ButtonGlitch() {
  useEffect(() => runButtonGlitch(), []);
  return null;
}
