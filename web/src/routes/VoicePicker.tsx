/** A voice field for Settings → AI → HQ calls. When the TTS server published its voices, a real
 *  dropdown of ALL of them: a `<datalist>` only suggests what matches the text already in the
 *  field, so a field holding "aria" offered nothing else and read as a broken dropdown, and on a
 *  phone it barely shows. Without a published list the voice is typed, as before. A saved voice the
 *  server no longer lists stays selectable, marked, so a pick is never silently replaced. */
import type { KeyboardEvent } from "react";

interface Props {
  /** The voices to offer. May be empty while a list exists (nothing in it can be picked yet). */
  voices: readonly string[];
  /** The server publishes NO voice list: the voice is typed. Never inferred from an empty
   *  `voices`, which can mean a list exists with nothing pickable in it (Hermes 7753). */
  typed: boolean;
  value: string;
  onChange: (voice: string) => void;
  /** Typed mode only: the field is left, or Enter pressed. A dropdown commits on change. */
  onCommit?: () => void;
  /** For a `<label htmlFor>`; else name it with `ariaLabel`. */
  id?: string;
  ariaLabel?: string;
  className: string;
  /** The first option, for "nothing chosen": e.g. "Default · ileana". Absent: no such option. */
  emptyLabel?: string;
  placeholder?: string;
  testId?: string;
}

export function VoicePicker({
  id,
  voices,
  typed,
  value,
  onChange,
  onCommit,
  ariaLabel,
  className,
  emptyLabel,
  placeholder,
  testId,
}: Props) {
  if (typed) {
    return (
      <input
        id={id}
        className={className}
        aria-label={ariaLabel}
        spellCheck={false}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onCommit}
        onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
          if (e.key === "Enter") onCommit?.();
        }}
        data-testid={testId}
      />
    );
  }
  const gone = value !== "" && !voices.includes(value);
  return (
    <select
      id={id}
      className={className}
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      data-testid={testId}
    >
      {emptyLabel !== undefined || value === "" ? (
        <option value="">{emptyLabel ?? "Choose a voice…"}</option>
      ) : null}
      {gone ? <option value={value}>{value} — not on your server</option> : null}
      {voices.map((v) => (
        <option key={v} value={v}>
          {v}
        </option>
      ))}
    </select>
  );
}
