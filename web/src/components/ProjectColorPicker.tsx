import { COLOR_PRESETS } from "../lib/projectColors";
import styles from "./ProjectColorPicker.module.css";


/** THE project colour picker (#1187): the six presets plus a clear, used by Settings → Projects
 *  and the New project wizard. It is the seam #571 extends with a custom hex input — one
 *  component, so both surfaces gain it together and no second picker can drift.
 *
 *  `value` is optional: Settings shows the picker as a one-shot action (pick → saved → closed) and
 *  passes none, so it looks exactly as it did; the wizard passes the draft's colour and the chosen
 *  swatch is marked. `size="large"` gives each swatch the 44 px touch target. */
export function ProjectColorPicker({
  label,
  value,
  onChange,
  disabled = false,
  clearLabel = "Clear",
  size = "small",
  clearClassName,
}: {
  /** The group's accessible name. */
  label: string;
  value?: string;
  onChange: (color: string) => void;
  disabled?: boolean;
  clearLabel?: string;
  size?: "small" | "large";
  /** The clear button borrows its host's button class, so Settings keeps its own. */
  clearClassName?: string;
}) {
  const selectable = value !== undefined;
  return (
    <div
      className={size === "large" ? `${styles.swatches} ${styles.large}` : styles.swatches}
      role="group"
      aria-label={label}
    >
      {COLOR_PRESETS.map((c) => (
        <button
          key={c}
          type="button"
          className={styles.swatch}
          style={{ background: c }}
          aria-label={`Color ${c}`}
          aria-pressed={selectable ? value?.toLowerCase() === c : undefined}
          disabled={disabled}
          onClick={() => onChange(c)}
        />
      ))}
      <button
        type="button"
        className={clearClassName ?? styles.clear}
        aria-pressed={selectable ? value === "" : undefined}
        disabled={disabled}
        onClick={() => onChange("")}
      >
        {clearLabel}
      </button>
    </div>
  );
}
