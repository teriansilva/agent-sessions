/** The disc beside a message (#1931). One component for the operator's API-session pane and the
 *  public share viewer, so the two can't drift; the share bundle imports it, so it imports nothing
 *  that talks to the operator's API (`shareBundle.test.ts`).
 *
 *  Decorative by design (`aria-hidden`): every message keeps its text label (`You`, `Jana · via
 *  public link`, `Agent`), so what a screen reader says does not change. */
import { useState } from "react";

import { whoColor, whoInitial } from "../../lib/who";
import styles from "./Avatar.module.css";

export type AvatarSpec =
  /** The operator's uploaded image; `fallback` is shown if it fails to load. */
  | { kind: "image"; src: string; fallback: string }
  /** A visitor: their initial on their name's colour. */
  | { kind: "who"; name: string }
  /** The operator without an image: an initial on the accent. */
  | { kind: "accent"; initial: string }
  /** Nobody can say who sent it. */
  | { kind: "unknown" }
  /** A paired instance (#2061): the linked-instances glyph in a dashed box. An origin, not a
   *  person and not the operator: it never takes the accent or a name's colour. */
  | { kind: "peer" }
  | { kind: "agent" };


export function Avatar({ spec, className }: { spec: AvatarSpec; className?: string }) {
  const [broken, setBroken] = useState<string | null>(null);
  const cls = className ? `${styles.avatar} ${className}` : styles.avatar;
  if (spec.kind === "image" && broken !== spec.src) {
    return (
      <span className={cls} data-kind="image" data-avatar="" aria-hidden="true">
        <img src={spec.src} alt="" referrerPolicy="no-referrer" onError={() => setBroken(spec.src)} />
      </span>
    );
  }
  if (spec.kind === "image" || spec.kind === "accent") {
    const initial = spec.kind === "image" ? spec.fallback : spec.initial;
    return (
      <span className={cls} data-kind="accent" data-avatar="" aria-hidden="true">
        {initial}
      </span>
    );
  }
  if (spec.kind === "who") {
    return (
      <span
        className={cls}
        data-kind="who"
        data-avatar=""
        aria-hidden="true"
        style={{ background: whoColor(spec.name) }}
      >
        {whoInitial(spec.name)}
      </span>
    );
  }
  if (spec.kind === "peer") {
    return (
      <span className={cls} data-kind="peer" data-avatar="" aria-hidden="true">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="3" y="4" width="7" height="7" />
          <rect x="14" y="13" width="7" height="7" />
          <path d="M10 7.5h4.5V13M14 16.5H9.5V11" />
        </svg>
      </span>
    );
  }
  if (spec.kind === "agent") {
    return (
      <span className={cls} data-kind="agent" data-avatar="" aria-hidden="true">
        🤖
      </span>
    );
  }
  return (
    <span className={cls} data-kind="neutral" data-avatar="" aria-hidden="true">
      ?
    </span>
  );
}
