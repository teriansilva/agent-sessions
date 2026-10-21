import type { ReactNode } from "react";
import {
  engineLabel,
  rosterReady,
  runtimeConfirmed,
  runtimeHint,
  useEngineRoster,
} from "../../app/engineRoster";
import { reloadRoster } from "../../app/reloadRoster";
import { ChatPane } from "../chat/ChatPane";
import { StructuredPane } from "../structured/StructuredPane";
import type { PaneHost } from "./paneHost";
import { OpeningPanel } from "./SessionOpening";
import styles from "./RuntimeGate.module.css";

/** Chooses a session's surface by its engine's RUNTIME (#853 §7). A `pty` engine gets its terminal
 *  (the children); a `chat` engine (#1209) gets the chat pane — BattleLab talks to its endpoint and
 *  keeps the conversation, so there is no terminal to attach. Any OTHER runtime gets an explicit
 *  panel, never a terminal: a terminal pane for it would show nothing and could not attach (the
 *  server refuses a non-`pty` engine at the ws route too, with 4415). An `api` engine (#1311) gets
 *  the structured pane: the native CLI runs in a contained worker, never in a terminal here.
 *
 *  The runtime is `runtimeHint` (#1502): the loaded roster's, or — before any roster has loaded —
 *  the one the last loaded roster recorded in this browser. So a returning browser draws the right
 *  pane on its FIRST render, with no roster round-trip, and a link to an API session never mounts a
 *  terminal first (whose refusal used to read "session not found"). With NO hint and no roster
 *  yet — only a browser that has never loaded one — a neutral "Opening…" waits for the roster, at
 *  most `OPENING_GIVE_UP_MS` before it offers Retry. A roster that is loaded but does not list the
 *  engine still renders the terminal as before: the server's `require_pty` is the boundary.
 *
 *  A hint can be stale; the roster always wins and this re-renders when it lands. A stale `pty`
 *  hint is corrected by the terminal's 4415; a stale `api` hint is why the structured pane is told
 *  `runtimeConfirmed={false}` until the roster says so — its failed first read is then
 *  provisional, never a verdict. The chat pane gets the same flag, for both stale directions
 *  between `chat` and `api`. `id` is the session's native id, needed by the chat pane. `host` is what the session's
 *  host hands the terminal (Files, To map, the window chrome slot); the structured pane takes the
 *  same set, so an API session gets the same head (#1332). */
export function RuntimeGate({
  engine,
  id,
  host,
  children,
}: {
  engine: string;
  id?: string;
  host?: PaneHost;
  children: ReactNode;
}) {
  useEngineRoster();
  const hint = runtimeHint(engine);
  if (hint === undefined) {
    // Loaded, and the roster does not list it: the terminal as before (the server decides).
    if (rosterReady()) return <>{children}</>;
    return <OpeningPanel reload={reloadRoster} />;
  }
  if (hint === "pty") return <>{children}</>;
  const runtime = hint;
  // Keyed by the engine-qualified session: every piece of session-owned state (conversation,
  // draft, pending sends) starts fresh on a switch, and a late completion from the previous
  // conversation lands on an unmounted pane, never on this one (Hermes on #1219).
  if (runtime === "chat" && id)
    return (
      <ChatPane
        key={`${engine}:${id}`}
        engine={engine}
        id={id}
        runtimeConfirmed={runtimeConfirmed(engine)}
      />
    );
  // A native API client (#1311): a BattleLab-owned structured view of the CLI it drives.
  if (runtime === "api" && id)
    return (
      <StructuredPane
        key={`${engine}:${id}`}
        engine={engine}
        id={id}
        host={host}
        runtimeConfirmed={runtimeConfirmed(engine)}
      />
    );
  // A runtime this build cannot draw is a verdict only once the roster says so; a cached one may
  // be stale, so it waits (bounded) like any other unconfirmed hint.
  if (!runtimeConfirmed(engine)) return <OpeningPanel reload={reloadRoster} />;
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
