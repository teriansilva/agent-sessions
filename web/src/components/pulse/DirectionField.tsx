/** The direction field: operator text, fact chips that insert placeholders, and a preview (#983 P2).
 *
 *  Used by the playbook editor (D1) and the per-mission Edit direction dialog (D2). Three rules:
 *
 *  - **Only the placeholders this objective's probe can fill are offered.** The list is the server's
 *    table (`mission_probes.placeholders`), never a copy kept here.
 *  - **The preview is the one renderer's output.** `POST /api/mission-directions/preview` runs
 *    `mission_directions.render` over example facts, so it also refuses an unknown placeholder in the
 *    save's own words before anything is saved. It is labelled as example facts, because it is not a
 *    claim about any mission.
 *  - **What the operator typed is sent as typed.** Nothing here trims, rewrites or fills the text. */
import { useEffect, useId, useRef, useState } from "react";

import { api, ApiError } from "../../lib/api";
import type { DirectionPlaceholder } from "../../types/api";

import d from "./direction.module.css";
import { insertPlaceholder, placeholdersFor } from "./directionPlaceholders";

type Preview =
  | { status: "empty" }
  | { status: "pending"; text: string | null }
  | { status: "ok"; text: string }
  | { status: "error"; error: string };

/** The preview for `direction` on a `probe` objective: debounced, and an answer for an older input
 *  never replaces a newer one. The last good text stays on screen while the next one is fetched. */
function useDirectionPreview(direction: string, probe: string): Preview {
  const key = JSON.stringify([probe, direction]);
  const [answer, setAnswer] = useState<{
    for: string;
    text: string | null;
    error: string | null;
  } | null>(null);
  const empty = !direction.trim();

  useEffect(() => {
    if (empty) return;
    let live = true;
    const timer = setTimeout(() => {
      api
        .previewDirection(direction, probe)
        .then((r) => {
          if (live) setAnswer({ for: key, text: r.text, error: null });
        })
        .catch((e: unknown) => {
          if (!live) return;
          setAnswer({
            for: key,
            text: null,
            error:
              e instanceof ApiError && e.message
                ? e.message
                : "The preview could not be filled right now.",
          });
        });
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [direction, probe, key, empty]);

  if (empty) return { status: "empty" };
  if (!answer || answer.for !== key) return { status: "pending", text: answer?.text ?? null };
  if (answer.error !== null) return { status: "error", error: answer.error };
  return { status: "ok", text: answer.text ?? "" };
}

export function DirectionField({
  value,
  onChange,
  probe,
  placeholders,
  label,
  hint,
  previewLabel = "Preview · with example facts",
  testId,
  disabled = false,
}: {
  value: string;
  onChange: (text: string) => void;
  /** The objective's probe kind: decides which facts can be inserted. */
  probe: string;
  placeholders: readonly DirectionPlaceholder[] | undefined;
  /** Follows "Direction" in the field's label. */
  label?: string;
  hint?: string;
  previewLabel?: string;
  /** Prefix for this field's testids. */
  testId: string;
  /** Freezes the text and its fact chips, e.g. while the text is being saved (#997): an edit made
   *  while a save is pending would be discarded when the save closes the dialog. */
  disabled?: boolean;
}) {
  const textId = useId();
  const hintId = useId();
  const ref = useRef<HTMLTextAreaElement>(null);
  const chips = placeholdersFor(placeholders, probe);
  const preview = useDirectionPreview(value, probe);

  const insert = (name: string) => {
    const el = ref.current;
    const next = insertPlaceholder(
      value,
      el?.selectionStart ?? value.length,
      el?.selectionEnd ?? value.length,
      name,
    );
    onChange(next.text);
    // Back to the text with the caret after the fact, so the operator keeps typing where they were.
    requestAnimationFrame(() => {
      const t = ref.current;
      if (!t) return;
      t.focus();
      t.setSelectionRange(next.caret, next.caret);
    });
  };

  return (
    <div className={d.field} data-testid={testId}>
      <label htmlFor={textId} className={d.label}>
        <b>Direction</b>
        {label ? ` · ${label}` : null}
      </label>
      <textarea
        id={textId}
        ref={ref}
        className={d.textarea}
        value={value}
        rows={3}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        aria-describedby={hint ? hintId : undefined}
        data-testid={`${testId}-text`}
      />
      {chips.length > 0 ? (
        <div className={d.chips} role="group" aria-label="Insert a checked fact">
          <span className={d.chipsLabel} aria-hidden="true">
            Insert a checked fact
          </span>
          {chips.map((p) => (
            <button
              key={p.name}
              type="button"
              className={d.chip}
              disabled={disabled}
              onClick={() => insert(p.name)}
              aria-label={`Insert {${p.name}}, ${p.hint}`}
              data-testid={`${testId}-chip-${p.name}`}
            >
              <span className={d.chipToken}>{`{${p.name}}`}</span>
              <small className={d.chipHint}>{p.hint}</small>
            </button>
          ))}
        </div>
      ) : (
        <p className={d.noFacts} data-testid={`${testId}-no-facts`}>
          This objective&rsquo;s check has no facts a direction can name.
        </p>
      )}
      <div className={d.previewLabel}>{previewLabel}</div>
      <p
        className={d.preview}
        data-state={preview.status}
        data-testid={`${testId}-preview`}
        aria-live="polite"
      >
        {preview.status === "empty"
          ? "No direction: mission control sends your default nudge."
          : preview.status === "error"
            ? preview.error
            : preview.status === "ok"
              ? preview.text
              : (preview.text ?? "Filling the preview…")}
      </p>
      {hint ? (
        <p id={hintId} className={d.hint}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}
