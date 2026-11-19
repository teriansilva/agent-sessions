import styles from "./Remote.module.css";

/** The linked-instances glyph: two boxes joined. Drawn here; nothing is fetched for it. */
export function PeerGlyph({ size = 10 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="3" y="4" width="7" height="7" />
      <rect x="14" y="13" width="7" height="7" />
      <path d="M10 7.5h4.5V13M14 16.5H9.5V11" />
    </svg>
  );
}

/** The screen glyph: a monitor. Drawn here; nothing is fetched for it. */
export function ScreenGlyph({ size = 10 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="3" y="4" width="18" height="12" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}

/** The SCREEN tag (#2060): beside the peer badge wherever a shared TERMINAL is named (row, map
 *  node, pane head). It says what kind of share this is, a screen to watch and not a conversation,
 *  so it is neutral ink like the badge: a kind is not a state. */
export function ScreenTag() {
  return (
    <span className={styles.kindTag} title="A terminal's screen, as text" data-testid="screen-tag">
      <ScreenGlyph />
      <span>Screen</span>
    </span>
  );
}

/** The peer badge (#2055): the glyph and the peer's name in a dashed hairline box, in neutral
 *  ink. It marks an ORIGIN, not a state, so it never takes a status colour or the accent. The
 *  name is a text node: whatever a peer is called, it is never markup or a class. */
export function PeerBadge({ name }: { name: string }) {
  return (
    <span className={styles.badge} title={`Shared by ${name}`} data-testid="peer-badge">
      <PeerGlyph />
      <span className={styles.badgeName}>{name}</span>
    </span>
  );
}
