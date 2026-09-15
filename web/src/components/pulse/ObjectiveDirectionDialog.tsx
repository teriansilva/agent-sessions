/** Edit direction: one objective's direction for THIS mission (#983 P2, D2 / B6).
 *
 *  A mission copies its playbook's directions when its objectives are created, and later playbook
 *  edits never change it. So the choices are exactly the server's ops: keep the copy, copy the
 *  playbook's current direction again (`reset_direction`), write one for this mission
 *  (`set_direction`), or have none (`clear_direction`), in which case a nudge types the operator's
 *  default nudge.
 *
 *  Drawn with the Hand off dialog's sheet (`HudDialog.module.css`), which is a bottom sheet at
 *  ≤800px, and its shared action buttons. A refusal is shown here in the server's own words and the
 *  dialog stays open with the operator's text.
 *
 *  **The modal contract is `useModalDrawer`'s, not a copy of it** (#997 review 4880). Portalled to
 *  `<body>`, so the hook's default isolation makes `#root` inert while the dialog is open; it moves
 *  focus in, contains Tab, closes on Escape and on a press outside, and returns focus to the ⋯ that
 *  opened it. Closing is refused while a save is pending.
 *
 *  **A PENDING SAVE FREEZES THE DIALOG.** Every control is disabled until the server answers,
 *  the direction text and its fact chips included: a success closes the dialog, so an edit made
 *  while the request was in flight would be silently dropped. With nothing enabled, focus is held on
 *  the dialog itself (`tabIndex={-1}`), which the shared trap keeps it on. */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useConfig } from "../../app/config";
import type { MissionObjective } from "../../types/api";

import dlg from "../HudDialog.module.css";
import { DirectionField } from "./DirectionField";
import d from "./direction.module.css";
import {
  type DirectionChoice,
  type DirectionOp,
  initialChoice,
  isCopied,
  opFor,
} from "./objectiveDirection";
import { useModalDrawer } from "./useModalDrawer";

export function ObjectiveDirectionDialog({
  objective,
  canReset,
  onApply,
  onClose,
}: {
  objective: MissionObjective;
  /** The mission has a playbook to copy from. The server still decides whether this objective is in it. */
  canReset: boolean;
  /** Send one op. Resolves to `null` when the server accepted it, or its refusal in its own words. */
  onApply: (op: DirectionOp) => Promise<string | null>;
  onClose: () => void;
}) {
  const placeholders = useConfig()?.mission_probes?.placeholders;
  const titleId = useId();
  const groupName = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  // The ⋯ trigger: the menu hands focus back to it before this dialog mounts.
  const [returnTo] = useState(() =>
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  const triggerRef = useRef<HTMLElement | null>(returnTo);
  const [initial] = useState<DirectionChoice>(() => initialChoice(objective));
  /** Focus starts on the choice the objective has now. */
  const initialFocusRef = useRef<HTMLInputElement>(null);
  const copied = isCopied(objective);
  const [choice, setChoice] = useState<DirectionChoice>(initial);
  const [draft, setDraft] = useState(objective.direction ?? "");
  const [busy, setBusy] = useState(false);
  /** The same fact as `busy`, readable from the hook's event handlers without re-subscribing. */
  const pending = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const title = objective.title ?? objective.key;
  const op = opFor(choice, objective, draft);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Escape and a press outside both come through here; neither may abandon a save in flight.
  const close = useCallback(() => {
    if (!pending.current) onClose();
  }, [onClose]);

  useModalDrawer({ active: true, panelRef, initialFocusRef, triggerRef, onClose: close });

  const submit = async (next: DirectionOp) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    // The control that was pressed is about to be disabled, which drops its focus. Hold focus on the
    // dialog itself instead, so it never lands on the page behind it.
    panelRef.current?.focus();
    const refusal = await onApply(next);
    if (!alive.current) return;
    pending.current = false;
    if (refusal === null) {
      onClose();
      return;
    }
    setBusy(false);
    setError(refusal);
  };

  const radio = (value: DirectionChoice) => ({
    type: "radio" as const,
    name: groupName,
    value,
    checked: choice === value,
    disabled: busy,
    onChange: () => setChoice(value),
  });

  return createPortal(
    <div className={dlg.backdrop}>
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={busy}
        tabIndex={-1}
        className={dlg.dialog}
        data-testid="direction-dialog"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Direction for this mission
          </span>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>OBJECTIVE //</span>
          <b className={dlg.fromTitle}>{title}</b>
        </p>
        <div className={d.choices} role="radiogroup" aria-labelledby={titleId}>
          {copied ? (
            <>
              <label className={d.choice} data-testid="direction-choice-keep">
                <input
                  {...radio("keep")}
                  ref={initial === "keep" ? initialFocusRef : undefined}
                  data-testid="direction-keep"
                />
                <span className={d.choiceText}>
                  Use the playbook&rsquo;s direction
                  <small>
                    copied when this mission was created · playbook edits do not
                    change it
                  </small>
                </span>
              </label>
              {choice === "keep" ? (
                <pre className={d.copied} data-testid="direction-copied">
                  {objective.direction}
                </pre>
              ) : null}
            </>
          ) : null}
          {canReset ? (
            <button
              type="button"
              className={d.link}
              disabled={busy}
              onClick={() => void submit({ op: "reset_direction", key: objective.key })}
              data-testid="direction-reset"
            >
              Reset to the playbook&rsquo;s current direction
            </button>
          ) : null}
          <label className={d.choice} data-testid="direction-choice-write">
            <input
              {...radio("write")}
              ref={initial === "write" ? initialFocusRef : undefined}
              data-testid="direction-write"
            />
            <span className={d.choiceText}>Write one for this mission</span>
          </label>
          {choice === "write" ? (
            <DirectionField
              value={draft}
              onChange={setDraft}
              probe={objective.probe}
              placeholders={placeholders}
              testId="direction-field"
              disabled={busy}
            />
          ) : null}
          <label className={d.choice} data-testid="direction-choice-none">
            <input
              {...radio("none")}
              ref={initial === "none" ? initialFocusRef : undefined}
              data-testid="direction-none"
            />
            <span className={d.choiceText}>
              No direction
              <small>mission control sends your default nudge</small>
            </span>
          </label>
        </div>
        <p className={dlg.help}>Only you can edit this. The AI never writes a direction.</p>
        {error ? (
          <p className={dlg.error} role="alert" data-testid="direction-dialog-error">
            {error}
          </p>
        ) : null}
        <div className={dlg.actions}>
          <button
            type="button"
            className={dlg.cancel}
            disabled={busy}
            onClick={onClose}
            data-testid="direction-cancel"
          >
            Cancel
          </button>
          <button
            type="button"
            className={dlg.go}
            disabled={busy || op === null}
            onClick={() => op && void submit(op)}
            data-testid="direction-save"
          >
            {busy ? "Saving…" : "Save direction"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
