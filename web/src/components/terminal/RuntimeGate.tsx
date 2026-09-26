import type { ReactNode } from "react";
import { engineLabel, engineInfo, runsInTerminal, useEngineRoster } from "../../app/engineRoster";
import styles from "./RuntimeGate.module.css";

/** Chooses a session's surface by its engine's RUNTIME (#853 §7, P4). A `pty` engine — every
 *  engine this build has — gets its terminal (the children). An engine the roster says runs some
 *  OTHER way gets an explicit panel, never a terminal: the API-only runtime is #853 P9, and a
 *  terminal pane for it would show nothing and could not attach (the server refuses a non-`pty`
 *  engine at the ws route too).
 *
 *  While the roster is still loading the terminal renders as it always has: every engine that
 *  exists is `pty`, and the server's `require_pty` is the boundary — holding every terminal for a
 *  roster fetch would slow every page load to protect a runtime that does not exist yet. */
export function RuntimeGate({ engine, children }: { engine: string; children: ReactNode }) {
  useEngineRoster();
  if (runsInTerminal(engine) !== false) return <>{children}</>;
  const runtime = engineInfo(engine)?.runtime ?? "unknown";
  return (
    <div className={styles.panel} role="status">
      <p className={styles.tag}>SESSION VIEW // RUNTIME {runtime.toUpperCase()}</p>
      <p>
        {engineLabel(engine)} sessions run as <code>{runtime}</code>, which needs a newer
        BattleLab to show. Nothing was started.
      </p>
    </div>
  );
}
