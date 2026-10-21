import { useEffect } from "react";

import { runButtonGlitch } from "./dataFlow";

// Occasional ambient button glitch (#211 Phase 4a) — the user-requested replacement for the
// old gold shine sweep. Every ~7–23s it briefly adds `.glitching` to a random visible
// enabled button control, which runs the cosmetic brightness keyframes (buttonGlitch.css). Never disables
// the button (no pointer-events change) and runs only in the Full motion mode (#1531). Renders
// nothing; mounted once at the app root. The timer lives in dataFlow.ts, shared with the
// standalone connect shell.
export function ButtonGlitch() {
  useEffect(() => runButtonGlitch(), []);
  return null;
}
