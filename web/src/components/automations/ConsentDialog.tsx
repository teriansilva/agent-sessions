/** The consent dialog (#1201 §2): what an automation will do with nobody watching, confirmed.
 *
 *  It opens for a first enable and for a save that WIDENS an enabled automation (the server's 422).
 *  Every line in it is the server's (`scope_lines`), and the digest it confirms is the server's
 *  (`scope_digest`) — so the operator agrees to exactly what the server will hold them to, and a
 *  scope that changed in between is a 409 that re-opens this dialog with the new lines rather than
 *  a silent approval of something else. What widened is named twice: highlighted in place, and
 *  listed in a "wider than last time" box, because a highlight alone is colour-only.
 *
 *  Confirm stays disabled until the box is ticked — consent is an act, not a default. The HUD
 *  dialog sheet and the one action-button definition; portalled to <body> with the page inert
 *  behind it, Escape and the backdrop cancel, focus lands on the checkbox. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { useInertBehind } from "../templates/useInertBehind";
import { widenedLine } from "../../lib/automations";
import type { ConsentRequired } from "../../types/automations";
import hud from "../HudDialog.module.css";
import styles from "./automations.module.css";

export function ConsentDialog({
  name,
  mode,
  consent,
  busy = false,
  error,
  onCancel,
  onConfirm,
  returnFocusTo,
  reason,
}: {
  name: string;
  /** Why it needs approval again (`reapproval_reason`), shown for a re-approval. */
  reason?: string;
  /** `enable` for a first enable or a re-approval; `save` for a widening save. */
  mode: "enable" | "save";
  consent: ConsentRequired;
  busy?: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: (digest: string) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const titleId = useId();
  const boxRef = useRef<HTMLInputElement>(null);
  // The tick is FOR a digest: a new scope (a 409 re-open) is unticked, because agreeing to the old
  // lines is not agreeing to these.
  const [agreedFor, setAgreedFor] = useState<string | null>(null);
  const agreed = agreedFor != null && agreedFor === consent.scope_digest;
  const widened = consent.widened ?? [];
  const lines = consent.scope_lines ?? [];

  useInertBehind(returnFocusTo);
  useEffect(() => {
    boxRef.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (!busy) onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel, busy]);

  // A save-mode dialog is only ever for an automation that is ALREADY enabled (a widening edit),
  // so it asks to save changes, never to "re-enable" something that is on.
  const title = mode === "save" ? `Save changes to “${name}”?` : `Enable “${name}”?`;
  const confirmLabel = mode === "save" ? "Save changes" : "Enable automation";

  return createPortal(
    <div
      className={hud.backdrop}
      onMouseDown={() => (busy ? undefined : onCancel())}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={hud.dialog}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="consent-dialog"
      >
        <div className={hud.head}>
          <span className={hud.tag}>Automation // {mode === "save" ? "save" : "enable"}</span>
          <button
            type="button"
            className={styles.dialogClose}
            aria-label="Close"
            onClick={onCancel}
            disabled={busy}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <h2 id={titleId} className={styles.dialogTitle}>
          {title}
        </h2>
        {reason && (
          <div className={styles.widerBox} data-testid="consent-reason">
            <span className={styles.widerTag}>Why it needs approval again</span>
            <p className={styles.dialogLede}>{reason}</p>
          </div>
        )}
        <p className={styles.dialogLede}>
          It will run whether or not you are here, with exactly this scope:
        </p>
        <ul className={styles.scopeList} data-testid="consent-scope">
          {lines.length === 0 && <li>Its inputs can’t be checked right now.</li>}
          {lines.map((line, i) => {
            const w = widenedLine(line, widened);
            // EVERY line in full, however long (#1252 review): approving with a hidden suffix is
            // approving something not shown. The dialog body scrolls; the text wraps.
            return (
              <li
                key={i}
                className={w ? styles.scopeWidened : undefined}
                data-widened={w ? "" : undefined}
              >
                <span className={styles.scopeText}>{line}</span>
                {w && <span className={styles.widenedTag}>Widened</span>}
              </li>
            );
          })}
        </ul>
        {widened.length > 0 && (
          <div className={styles.widerBox} data-testid="consent-widened">
            <span className={styles.widerTag}>Wider than last time</span>
            <ul>
              {widened.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}
        <label className={styles.agree}>
          <input
            ref={boxRef}
            type="checkbox"
            checked={agreed}
            onChange={(e) => setAgreedFor(e.target.checked ? (consent.scope_digest ?? null) : null)}
            disabled={busy || !consent.scope_digest}
            data-testid="consent-agree"
          />
          <span>I understand this runs while I’m away, with the scope above.</span>
        </label>
        {error && (
          <p className={hud.error} role="alert">
            {error}
          </p>
        )}
        <div className={hud.actions}>
          <button type="button" className={hud.cancel} onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={hud.go}
            disabled={!agreed || busy || !consent.scope_digest}
            onClick={() => consent.scope_digest && onConfirm(consent.scope_digest)}
            data-testid="consent-confirm"
          >
            {busy ? "Saving…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
