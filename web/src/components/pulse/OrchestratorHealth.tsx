/** The orchestrator's DEGRADED state, on the console, read-only (#929).
 *
 *  #929 moved every autonomy *control* into Settings, which is where the tier and threshold
 *  already lived. The health warning deliberately did **not** go with them.
 *
 *  **Why this exists at all.** #772's whole point is the difference between "nothing needed your
 *  attention" and "nothing has been LOOKED AT since yesterday evening". Those render identically
 *  on a quiet page. If the only outage signal lives in Settings, an operator working the console
 *  — which is where they are, because it is the page the work is on — sees a calm mission list
 *  and cannot tell which of the two they are looking at. Moving the warning would have restored
 *  the exact failure #772 was filed to remove.
 *
 *  So: configuration moved, evidence stayed. This renders nothing at all when the orchestrator is
 *  healthy or off, carries no controls, and links to the panel that can actually fix it.
 *
 *  It refetches on `refreshKey` for the same reason the old strip did — a pass run from Settings,
 *  or an action settled on a card, is newer evidence than whatever this last saw. */
import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { relTime } from "../../lib/format";
import type { AiTaskLast } from "../../types/api";
import styles from "./Orchestrator.module.css";

export function OrchestratorHealth({
  refreshKey = 0,
}: {
  refreshKey?: number;
}) {
  const [health, setHealth] = useState<AiTaskLast | undefined>(undefined);

  useEffect(() => {
    let live = true;
    // Defensive, exactly as the strip was: this is one line on a page that must render without
    // it. A throw — endpoint down, route missing on an older server — degrades to "no badge",
    // never to a blank console.
    Promise.resolve()
      .then(() => api.orchestrator())
      .then((s) => live && setHealth(s.last?.orchestrator))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [refreshKey]);

  // TWO consecutive failures — a first failed pass stays silent, including the first ever.
  // `consecutive_failures` is SERVER-owned so the client is not inventing its own definition of
  // an outage, and one failure says nothing because a blip that shouts is a badge people learn
  // to ignore. (The previous wording here said "or a first-ever run that failed", which the
  // `fails >= 2` below has never done — #930 review 3.)
  const fails = health?.consecutive_failures ?? 0;
  const degraded = !!health && !health.ok && fails >= 2;
  if (!degraded) return null;

  const lastOkAgo = health?.last_ok ? relTime(health.last_ok) : "";
  return (
    <p className={styles.degraded} role="status" data-testid="orch-degraded">
      <AlertTriangle size={13} aria-hidden="true" />
      <span>
        The orchestrator can’t reach its AI endpoint
        {lastOkAgo
          ? ` — last successful pass ${lastOkAgo}`
          : " — no pass has succeeded yet"}
        .{health?.error ? ` ${health.error}` : ""}{" "}
        <Link to="/settings/ai-review">Check Settings → AI Review</Link>.
      </span>
    </p>
  );
}
