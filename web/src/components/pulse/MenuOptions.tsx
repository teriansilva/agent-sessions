/** Answer an escalated menu from the console (#1060 Phase 3).
 *
 *  Shown only on an escalation whose server-parsed `observed_prompt.menu` has options. One button
 *  per option, labelled with the agent's own words — display text only. The first tap ARMS ("Send
 *  2"), the second sends: typing into a live session deserves the same two steps an approval gets.
 *
 *  What a tap sends is decided server-side (`menu_answer`): the option number AND the label shown
 *  here must still match the menu on the live screen, and the payload is the digit alone. So the
 *  card never needs to know the screen moved — the server refuses, and the refusal is shown
 *  verbatim. Nothing here opens the session or attaches a viewer. */
import { useState } from "react";

import { api, ApiError } from "../../lib/api";
import type { OrchestratorAction } from "../../types/api";

import styles from "./Orchestrator.module.css";

export function MenuOptions({
  action,
  onResolved,
  onNote,
}: {
  action: OrchestratorAction;
  onResolved?: (a: OrchestratorAction) => void;
  onNote?: (msg: string) => void;
}) {
  const menu = action.observed_prompt?.menu;
  const [armed, setArmed] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  if (!menu || !menu.options.length) return null;

  const send = async (n: number, label: string) => {
    if (busy) return;
    if (armed !== n) {
      setArmed(n);
      setNote(null);
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const r = await api.chooseAction(action.id, n, label);
      onResolved?.(r);
    } catch (e) {
      setArmed(null);
      const state =
        e instanceof ApiError
          ? (e.record as { state?: unknown } | undefined)?.state
          : undefined;
      if (
        e instanceof ApiError &&
        e.status === 502 &&
        state === "indeterminate"
      ) {
        // MAY HAVE LANDED: never invite a blind retry of a keypress.
        setNote(e.message);
      } else if (e instanceof ApiError && e.message) {
        // Every definite refusal reaches the console too, not only a 409 (#1082 review): a 503
        // (ledger unreadable) or 502 (could not be recorded) is as much "nothing was sent".
        setNote(`Not sent — ${e.message}`);
        onNote?.(`Not sent — ${e.message}`);
      } else {
        setNote("Couldn’t complete that — please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.menu} data-testid="menu-options">
      <p className={styles.menuQ}>
        <span className={styles.menuLead}>The session is asking:</span>{" "}
        {menu.question}
      </p>
      <div
        className={styles.menuBtns}
        role="group"
        aria-label="Answer the session's question"
      >
        {menu.options.map((o) => (
          <button
            key={o.n}
            type="button"
            className={armed === o.n ? styles.approve : styles.menuOpt}
            disabled={busy}
            aria-pressed={armed === o.n}
            data-testid="menu-option"
            onClick={() => void send(o.n, o.label)}
          >
            {armed === o.n
              ? busy
                ? "Sending…"
                : `Send ${o.n} · ${o.label}`
              : `${o.n}. ${o.label}`}
          </button>
        ))}
      </div>
      {armed !== null && !busy ? (
        <p className={styles.menuHint} data-testid="menu-armed">
          Types {armed} into the session — nothing else. Tap again to send.
        </p>
      ) : null}
      {note ? (
        <p className={styles.stale} data-testid="menu-note">
          {note}
        </p>
      ) : null}
    </div>
  );
}
