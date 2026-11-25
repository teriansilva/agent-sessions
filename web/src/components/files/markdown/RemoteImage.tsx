import styles from "./markdownDocument.module.css";

/** A remote image is never fetched: a document an agent wrote must not turn opening it into a
 *  request to a third party. It shows as a placeholder naming the host, with a link out. */
export function RemoteImage({ href, host, alt }: { href: string; host: string; alt?: string }) {
  const label = alt?.trim() || "";
  return (
    <span className={styles.remoteImage} data-md-remote-image={host}>
      <span className={styles.tag}>Remote image</span>
      <span>
        {label ? `${label} — ` : ""}from {host}, not loaded here.
      </span>
      <a href={href} target="_blank" rel="noopener noreferrer">
        Open link ↗
      </a>
    </span>
  );
}
