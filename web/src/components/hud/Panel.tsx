/** NIGHTJAR panel and callsign (#1531, docs/design.md v2 §6) — the armour panel every surface
 *  sits on, packaged so a surface never hand-pastes the edge / face / rail / tick spans.
 *
 *  A panel CLIPS (its chamfer is a static `clip-path`), so a menu or tooltip that must escape it
 *  is portalled. Its glow, when asked for, lives on a wrapper for the same reason. */
import type { CSSProperties, ElementType, ReactNode } from "react";

export type PanelTone = "default" | "hero" | "quiet" | "alert";

export function Panel({
  as: Tag = "section",
  tone = "default",
  rail = "full",
  liveRail = false,
  ticks = true,
  small = false,
  glow = false,
  powerOn = false,
  className = "",
  faceClassName = "",
  style,
  children,
  ...rest
}: {
  as?: ElementType;
  tone?: PanelTone;
  /** The lit line along the top edge: the whole width, a third of it, or none. */
  rail?: "full" | "short" | "none";
  /** A data pulse runs along the rail (Full mode only; ambient). */
  liveRail?: boolean;
  ticks?: boolean;
  /** Control-sized chamfer (`--cut-sm`). */
  small?: boolean;
  /** The panel glow (dark only, `--glow-panel`), on a wrapper. */
  glow?: boolean;
  /** Fade + rise + one-shot scan when it mounts. */
  powerOn?: boolean;
  className?: string;
  faceClassName?: string;
  style?: CSSProperties;
  children?: ReactNode;
  [attr: `data-${string}`]: string | undefined;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  role?: string;
  id?: string;
}) {
  const cls = [
    "nj-panel",
    tone !== "default" ? `is-${tone}` : "",
    small ? "is-sm" : "",
    powerOn ? "nj-power-on nj-fade-ok" : "",
    glow ? "" : className,
  ]
    .filter(Boolean)
    .join(" ");
  const panel = (
    <Tag className={cls} style={glow ? undefined : style} {...rest}>
      <div className={`nj-face ${faceClassName}`.trim()}>
        {rail !== "none" && (
          <span
            className={`nj-rail${rail === "short" ? " is-short" : ""}${liveRail ? " is-live" : ""}`}
            aria-hidden="true"
          />
        )}
        {ticks && <span className="nj-tick tr" aria-hidden="true" />}
        {children}
      </div>
    </Tag>
  );
  if (!glow) return panel;
  return (
    <div className={`nj-glow ${className}`.trim()} style={style}>
      {panel}
    </div>
  );
}

/** `[notch] TITLE // value // meta` — the callsign header. */
export function Callsign({
  title,
  value,
  meta,
  notch = true,
  as: Tag = "div",
  className = "",
  id,
}: {
  title: ReactNode;
  value?: ReactNode;
  meta?: ReactNode;
  notch?: boolean;
  as?: ElementType;
  className?: string;
  id?: string;
}) {
  return (
    <Tag className={`nj-callsign ${className}`.trim()} id={id}>
      {notch && <span className="nj-notch" aria-hidden="true" />}
      <span className="nj-title">{title}</span>
      {value !== undefined && (
        <>
          <span aria-hidden="true">//</span>
          <span className="nj-value">{value}</span>
        </>
      )}
      {meta !== undefined && <span className="nj-meta">{meta}</span>}
    </Tag>
  );
}
