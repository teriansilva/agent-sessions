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
