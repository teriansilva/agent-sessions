/** NIGHTJAR LEDs, meters and stamps (#1531, docs/design.md v2 §6). Status colour is
 *  load-bearing: an LED or a stamp always carries its word, never colour alone. */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { useMotion } from "../../theme/motionStore";

export type LedTone = "accent" | "up" | "att" | "down" | "idle" | "draft";

/** Remembers the previous value and reports `true` for one arrival window after it changes. The
 *  first render never counts as a change. Always false outside Full and Reduced fades are done
 *  by CSS, so this only drives the decorative flash. */
function useChanged<T>(value: T): boolean {
  const prev = useRef(value);
  const [changed, setChanged] = useState(false);
  useEffect(() => {
    if (Object.is(prev.current, value)) return;
    prev.current = value;
    setChanged(true);
    const t = setTimeout(() => setChanged(false), 520);
    return () => clearTimeout(t);
  }, [value]);
  return changed;
}

export function Led({
  tone = "accent",
  label,
  className = "",
}: {
  tone?: LedTone;
  /** The word that carries the state; rendered visually hidden when the LED sits beside its own
   *  visible label (pass `label` only when there is none). */
  label?: string;
  className?: string;
}) {
  const changed = useChanged(tone);
  const toneCls = tone === "accent" ? "" : tone === "att" ? "attention" : tone;
  return (
    <span
      className={`hud-led ${toneCls} ${changed ? "is-changed" : ""} ${className}`.replace(/\s+/g, " ").trim()}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : "true"}
      data-tone={tone}
    />
  );
}

export function Pips({
  value,
  max = 5,
  tone,
  label,
}: {
  value: number;
  max?: number;
  tone?: "ok" | "warn" | "down";
  label: string;
}) {
  const n = Math.max(0, Math.min(max, Math.round(value)));
  return (
    <span
      className={`nj-pips${tone ? ` is-${tone}` : ""}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={n}
    >
      {Array.from({ length: max }, (_, i) => (
        <i key={i} className={i < n ? "on" : undefined} />
      ))}
    </span>
  );
}

export function Gauge({
  value,
  segments = 20,
  low = 0.2,
  label,
  ticks,
}: {
  /** 0..1 */
  value: number;
  segments?: number;
  /** At or under this fraction the gauge reads low (degraded tone, a slow pulse in Full). */
  low?: number;
  label: string;
  ticks?: ReactNode[];
}) {
  const { ambient } = useMotion();
  const v = Math.max(0, Math.min(1, value));
  const on = Math.round(v * segments);
  const isLow = v <= low;
  return (
    <div
      className={`nj-gauge${isLow ? " is-low" : ""}${isLow && ambient ? " is-live" : ""}`}
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(v * 100)}
    >
      <div className="nj-track" aria-hidden="true">
        {Array.from({ length: segments }, (_, i) => (
          <i key={i} className={i < on ? "on" : undefined} />
        ))}
      </div>
      {ticks && ticks.length > 0 && (
        <div className="nj-ticks" aria-hidden="true">
          {ticks.map((t, i) => (
            <span key={i}>{t}</span>
          ))}
        </div>
      )}
    </div>
  );
}

/** A display readout. A changed value flashes once (a filter step on the number, never a
 *  scale); the value itself is in the DOM at once. */
export function Readout({
  value,
  unit,
  small = false,
  className = "",
  testId,
}: {
  value: ReactNode;
  unit?: ReactNode;
  small?: boolean;
  className?: string;
  testId?: string;
}) {
  const changed = useChanged(typeof value === "string" || typeof value === "number" ? value : null);
  return (
    <span
      className={`nj-readout${small ? " is-sm" : ""}${changed ? " is-changed" : ""} ${className}`.trim()}
      data-testid={testId}
    >
      {value}
      {unit !== undefined && <span className="nj-unit">{unit}</span>}
    </span>
  );
}

export function Stamp({
  tone,
  children,
}: {
  tone?: "ok" | "warn" | "down" | "hud";
  children: ReactNode;
}) {
  return <span className={`nj-stamp${tone ? ` is-${tone}` : ""}`}>{children}</span>;
}
