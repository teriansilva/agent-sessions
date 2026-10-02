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
import type { OrchestratorAction, PermissionPrompt } from "../../types/api";

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
  const permission = action.observed_prompt?.permission;
  if (permission && permission.options.length) {
    return (
      <PermissionOptions
        action={action}
        prompt={permission}
        onResolved={onResolved}
        onNote={onNote}
      />
    );
  }
  return <MenuButtons action={action} onResolved={onResolved} onNote={onNote} />;
}

function MenuButtons({
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

/** A TOOL-PERMISSION dialog, answered from the card (#1213).
 *
 *  The dialog the server read off the screen — which engine, which tool, the command / pattern /
 *  path in full — and one button per option the agent offers. Arm, then send, as a menu answer: a
 *  permission is typed into a live agent. An option that grants more than this one call ("Allow
 *  always", "don't ask again") says so when armed, before it can be sent.
 *
 *  What a tap sends is decided by the SERVER: it re-parses the live screen, refuses unless it is
 *  still this exact dialog, and builds the keys from the option and the dialog's own cursor. The
 *  card only names the option and the label it showed. After a send it stops offering buttons and
 *  says what was sent; after an uncertain send it says so and offers no retry. */
function PermissionOptions({
  action,
  prompt,
  onResolved,
  onNote,
}: {
  action: OrchestratorAction;
  prompt: PermissionPrompt;
  onResolved?: (a: OrchestratorAction) => void;
  onNote?: (msg: string) => void;
}) {
  const [armed, setArmed] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  /** Terminal for this card: `sent` (delivered) or `unknown` (may have landed). */
  const [done, setDone] = useState<{ kind: "sent" | "unknown"; label: string } | null>(
    null,
  );
  const engine = prompt.engine || "the agent";
  const armedOpt = prompt.options.find((o) => o.n === armed) ?? null;

  const send = async (n: number, label: string) => {
    if (busy || done) return;
    if (armed !== n) {
      setArmed(n);
      setNote(null);
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const r = await api.chooseAction(action.id, n, label);
      setDone({ kind: "sent", label });
      onResolved?.(r);
    } catch (e) {
      setArmed(null);
      const record =
        e instanceof ApiError
          ? (e.record as { state?: unknown; detail?: unknown } | undefined)
          : undefined;
      // ONLY THE SERVER'S OWN REFUSAL MEANS NOTHING WAS SENT (#1218 review): a structured body
      // with its `detail` and no `indeterminate` state. Anything else — a gateway 504, a dropped
      // connection, an unreadable body — says nothing about whether the keys went out, so it is
      // treated exactly like an answer that may have landed: no retry from this card.
      if (
        e instanceof ApiError &&
        typeof record?.detail === "string" &&
        record.state !== "indeterminate"
      ) {
        setNote(`Not sent — ${e.message}`);
        onNote?.(`Not sent — ${e.message}`);
      } else {
        setDone({ kind: "unknown", label });
        setNote(
          "The answer may or may not have reached the session. Check the session before answering again — this card will not resend it.",
        );
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.perm} data-testid="permission-card">
      <div className={styles.permHead}>
        <span className={styles.permKind}>
          {prompt.heading === "Always allow"
            ? `${engine} asks to confirm`
            : `${engine} asks permission`}
        </span>
        <span className={styles.permTool}>{prompt.title}</span>
      </div>
      {prompt.detail ? (
        <pre className={styles.permDetail} data-testid="permission-detail">
          {prompt.detail}
        </pre>
      ) : null}
      {prompt.question ? <p className={styles.permQ}>{prompt.question}</p> : null}
      {done ? null : (
        <div
          className={styles.permBtns}
          role="group"
          aria-label={`Answer ${engine}'s permission prompt`}
        >
          {prompt.options.map((o) => (
            <button
              key={o.n}
              type="button"
              className={
                armed === o.n
                  ? styles.approve
                  : `${styles.menuOpt} ${o.persistent ? styles.permPersistent : ""}`
              }
              disabled={busy}
              aria-pressed={armed === o.n}
              data-testid="permission-option"
              data-persistent={o.persistent ? "true" : undefined}
              onClick={() => void send(o.n, o.label)}
            >
              {armed === o.n ? (busy ? "Sending…" : `Send · ${o.label}`) : o.label}
            </button>
          ))}
        </div>
      )}
      {armedOpt && !busy && !done ? (
        <p className={styles.menuHint} data-testid="permission-armed">
          Chooses “{armedOpt.label}” in {engine}’s own dialog — the server checks the live
          screen first. Tap again to send.
        </p>
      ) : null}
      {armedOpt?.persistent && !done ? (
        <p className={styles.permWarn} data-testid="permission-warning">
          <strong>Persistent.</strong> “{armedOpt.label}” lets this agent do more than this one
          call without asking again, for as long as its own rule lasts.
        </p>
      ) : null}
      {done?.kind === "sent" ? (
        <p className={styles.permDone} data-testid="permission-done">
          You chose <strong>{done.label}</strong> · sent
        </p>
      ) : null}
      {note ? (
        <p className={styles.stale} data-testid="permission-note">
          {note}
        </p>
      ) : null}
    </div>
  );
}
