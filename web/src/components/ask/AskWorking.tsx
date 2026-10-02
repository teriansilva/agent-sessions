/** The pending answer (#1171): a scan bar that moves, the step the ask is on, and how long it has
 *  been at it. It replaced a static `…` that looked the same whether the ask had just started or
 *  had been stuck for a minute.
 *
 *  The words come from the server's own progress events, so they say what is actually happening
 *  — how many sessions the catalog holds, how many transcripts Stage 2 is reading — not a
 *  rotation of reassuring phrases. `role="status"` lets a screen reader hear a step change
 *  without the ticking seconds (they are `aria-hidden`), and all motion stops under
 *  `prefers-reduced-motion` (AskConsole.module.css).
 */
import { useEffect, useState } from "react";

import a from "./AskConsole.module.css";
import { askStepLabel, type AskStep } from "./askStep";

export function AskWorking({ step }: { step: AskStep | null }) {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.floor((now - started) / 1000));
  return (
    <div className={a.working} data-testid="ask-working">
      <div className={a.scan} aria-hidden="true">
        <span className={a.scanBlock} />
      </div>
      <div className={a.workingRow}>
        <span role="status" className={a.step} data-testid="ask-step">
          {askStepLabel(step)}
        </span>
        <span className={a.elapsed} aria-hidden="true">
          {secs}s
        </span>
      </div>
    </div>
  );
}
