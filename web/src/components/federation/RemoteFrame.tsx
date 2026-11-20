import type { ReactNode, Ref } from "react";
import { Led } from "../hud/Meters";
import { PeerBadge } from "./PeerBadge";
import styles from "./Remote.module.css";
import together from "./Together.module.css";

/** The frame every state of a remote pane is drawn in (#2055): dashed, with the banner. Both
 *  panes use it: the conversation of an API session (`RemotePane`) and the screen of a terminal
 *  (`RemoteScreenPane`, #2060), which passes its tag and says "screen only". The frame itself has
 *  no composer: only `RemotePane` passes one, as its `foot`, for an API share whose mirrored level
 *  is Suggest or Send (#2061); the screen pane never does. The same holds for working together
 *  (#2062): the ⋯ menu, the presence line and the pane ref are passed by `RemotePane` only, so a
 *  terminal's screen has none of them. */
export function RemoteFrame({
  title,
  peer,
  until,
  led = "idle",
  word,
  access = "view only",
  tag,
  empty,
  notices,
  children,
  foot,
  menu,
  presence,
  paneRef,
}: {
  title: string;
  peer: string | null;
  /** When the share ends: a date ("22 Nov", shown as "until 22 Nov"), or a grant's end as
   *  `grantEnds` says it, whose countdown ("ends in 42 min") is set in bold (#2061). */
  until?: string | { text: string; countdown: boolean } | null;
  led?: "up" | "idle";
  word: string;
  /** What the share allows ("view only", "can suggest", "can send", "screen only"), in the
   *  banner and the pane's accessible name. */
  access?: string;
  /** Beside the title, before the peer badge: what kind of share this is, when it has a tag. */
  tag?: ReactNode;
  empty?: string;
  /** Lines of the pane's own under the banner (a reloaded pane's notices, #2061): text only. */
  notices?: readonly string[];
  children?: ReactNode;
  foot?: ReactNode;
  /** The pane's ⋯ menu (#2062), after the badge. `RemotePane` only. */
  menu?: ReactNode;
  /** "<owner>'s operator is here" (#2062): a name and one bit, while the owner says so. */
  presence?: string | null;
  /** The pane's own element, the notes drawer's anchor (#2062). */
  paneRef?: Ref<HTMLElement>;
}) {
  const ends = typeof until === "string" ? { text: `until ${until}`, countdown: false } : until;
  return (
    <div className={styles.paneWrap}>
      <section
        ref={paneRef}
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
          {menu}
        </header>
        <div className={styles.banner} role="note" data-testid="remote-banner">
          <span className={styles.bannerText}>
            Shared by <b>{peer ?? "another instance"}</b>
            {ends && (
              <>
                {` · ${access} · `}
                {ends.countdown ? <b data-testid="remote-countdown">{ends.text}</b> : ends.text}
              </>
            )}
          </span>
          {presence && (
            <span className={`${styles.state} ${together.presence}`} data-testid="remote-presence">
              <Led tone="up" />
              <span className={styles.wordOk}>{presence}</span>
            </span>
          )}
          <span
            className={`${styles.state} ${presence ? together.presenceNext : ""}`}
            data-testid="remote-state"
          >
            <Led tone={led} />
            <span className={led === "up" ? styles.wordOk : undefined}>{word}</span>
          </span>
        </div>
        {(notices ?? []).map((text) => (
          <p key={text} className={styles.paneNote} role="status" data-testid="remote-reload-notice">
            {text}
          </p>
        ))}
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
