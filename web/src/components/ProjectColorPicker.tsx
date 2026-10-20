import type { CSSProperties } from "react";
import { PROJECT_COLORS } from "../lib/projectColors";
import { onRadioGroupKeyDown, rovingTabIndex } from "../lib/radioArrows";
import { normalizeAccent, onAccentFor } from "../theme/accent";
import styles from "./ProjectColorPicker.module.css";


/** THE project colour picker (#1187), used by Settings → Projects and the New project wizard. It
 *  draws the accent wheel the way Settings → Interface → Accent does (#1508): 44 px named squares,
 *  a check on the chosen one, one radiogroup that Tab enters once and the arrows move through.
 *  It is the seam #571 extends with a custom hex input — one component, so both surfaces gain it
 *  together and no second picker can drift.
 *
 *  A stored colour that is not one of the sixteen (the old palette, or a hand-set hex) is shown
 *  first as the CURRENT swatch, checked, so opening the picker never drops it silently. */
export function ProjectColorPicker({
  label,
  value,
  onChange,
  disabled = false,
  clearLabel = "None",
}: {
  /** The group's accessible name. */
  label: string;
  /** The colour in force: `""` = none. */
  value: string;
  onChange: (color: string) => void;
  disabled?: boolean;
  clearLabel?: string;
}) {
  const current = normalizeAccent(value) ?? "";
  const custom = current && !PROJECT_COLORS.some((p) => p.hex === current) ? current : "";
  const options = [
    ...(custom ? [{ hex: custom, label: `Current colour ${custom}` }] : []),
    ...PROJECT_COLORS.map((p) => ({ hex: p.hex, label: p.label })),
  ];
  return (
    <div
      className={styles.picker}
      role="radiogroup"
      aria-label={label}
      onKeyDown={onRadioGroupKeyDown}
    >
      <div className={styles.swatches}>
        {options.map((o, i) => {
          const on = current === o.hex;
          return (
            <button
              key={o.hex}
              type="button"
              role="radio"
              aria-checked={on}
              aria-label={o.label}
              title={o.label}
              tabIndex={rovingTabIndex(on, i, true)}
              className={on ? `${styles.swatch} ${styles.on}` : styles.swatch}
              style={{ "--dot": o.hex, "--dot-ink": onAccentFor(o.hex) } as CSSProperties}
              disabled={disabled}
              onClick={() => onChange(o.hex)}
            />
          );
        })}
      </div>
      <button
        type="button"
        role="radio"
        aria-checked={current === ""}
        tabIndex={rovingTabIndex(current === "", options.length, true)}
        className={styles.clear}
        disabled={disabled}
        onClick={() => onChange("")}
      >
        {clearLabel}
      </button>
    </div>
  );
}
