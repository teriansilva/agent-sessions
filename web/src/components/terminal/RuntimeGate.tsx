import type { ReactNode } from "react";
import { engineLabel, engineInfo, runsInTerminal, useEngineRoster } from "../../app/engineRoster";
import { ChatPane } from "../chat/ChatPane";
import styles from "./RuntimeGate.module.css";

/** Chooses a session's surface by its engine's RUNTIME (#853 §7). A `pty` engine gets its terminal
 *  (the children); a `chat` engine (#1209) gets the chat pane — BattleLab talks to its endpoint and
 *  keeps the conversation, so there is no terminal to attach. Any OTHER runtime gets an explicit
 *  panel, never a terminal: a terminal pane for it would show nothing and could not attach (the
 *  server refuses a non-`pty` engine at the ws route too).
 *
 *  While the roster is still loading the terminal renders as it always has: the server's
 *  `require_pty` is the boundary, and holding every terminal for a roster fetch would slow every
 *  page load. `id` is the session's native id, needed by the chat pane. */
export function RuntimeGate({
  engine,
  id,
  children,
}: {
  engine: string;
  id?: string;
  children: ReactNode;
}) {
  useEngineRoster();
  if (runsInTerminal(engine) !== false) return <>{children}</>;
  const runtime = engineInfo(engine)?.runtime ?? "unknown";
  // Keyed by the engine-qualified session: every piece of session-owned state (conversation,
  // draft, pending sends) starts fresh on a switch, and a late completion from the previous
  // conversation lands on an unmounted pane, never on this one (Hermes on #1219).
  if (runtime === "chat" && id)
    return <ChatPane key={`${engine}:${id}`} engine={engine} id={id} />;
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
