import type { ReactNode } from "react";
import { Led } from "../hud/Meters";
import { PeerBadge } from "./PeerBadge";
import styles from "./Remote.module.css";

/** The frame every state of a remote pane is drawn in (#2055): dashed, with the banner, never a
 *  composer. Both panes use it: the conversation of an API session (`RemotePane`) and the screen
 *  of a terminal (`RemoteScreenPane`, #2060), which passes its tag and says "screen only". */
export function RemoteFrame({
  title,
  peer,
  until,
  led = "idle",
  word,
  access = "view only",
  tag,
  empty,
  children,
  foot,
}: {
  title: string;
  peer: string | null;
  until?: string | null;
  led?: "up" | "idle";
  word: string;
  /** What the share allows, in the banner and the pane's accessible name. */
  access?: string;
  /** Beside the title, before the peer badge: what kind of share this is, when it has a tag. */
  tag?: ReactNode;
  empty?: string;
  children?: ReactNode;
  foot?: ReactNode;
}) {
  return (
    <div className={styles.paneWrap}>
      <section
        className={styles.pane}
        aria-label={`${title}, shared by ${peer ?? "another instance"}, ${access}`}
        data-testid="remote-pane"
      >
        <header className={styles.head}>
          <h1 className={styles.title} data-testid="remote-title">
            {title}
          </h1>
          {tag}
          {peer && <PeerBadge name={peer} />}
        </header>
        <div className={styles.banner} role="note" data-testid="remote-banner">
          <span className={styles.bannerText}>
            Shared by <b>{peer ?? "another instance"}</b>
            {until ? ` · ${access} · until ${until}` : ""}
          </span>
          <span className={styles.state} data-testid="remote-state">
            <Led tone={led} />
            <span className={led === "up" ? styles.wordOk : undefined}>{word}</span>
          </span>
        </div>
        {empty !== undefined ? (
          <div className={styles.empty} role="status" data-testid="remote-empty">
            <p>{empty}</p>
          </div>
        ) : (
          children
        )}
        {foot}
      </section>
    </div>
  );
}
