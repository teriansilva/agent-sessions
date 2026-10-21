/** Settings → Updates: what this build brought, and where the full notes are (#1085).
 *
 *  The summary is the newest release BUNDLED in this build (`whatsnew/releases.ts`) — the build
 *  only knows its own notes, and fetching another version's from the server would be a new
 *  outbound call. So it describes what you have, and the button opens the full What's new dialog.
 *
 *  **The GitHub link is stable-only.** A stable install runs a tagged release, which has a release
 *  page: the available one when an update is offered, otherwise the installed one. The main
 *  channel runs a commit of the development branch, which has no release and no notes, so it
 *  says that instead of linking to a page that does not describe it.
 */
import { ExternalLink, Sparkles } from "lucide-react";

import { releaseNotesUrl } from "../../lib/links";
import { newestRelease, releaseLabel } from "../../whatsnew/due";
import { RELEASES, type WhatsNewRelease } from "../../whatsnew/releases";

import styles from "./UpdateWhatsNew.module.css";

export function UpdateWhatsNew({
  channel,
  current,
  available,
  onOpen,
  releases = RELEASES,
}: {
  channel: string;
  /** The running version (`/api/version`). */
  current: string | null;
  /** The channel's latest ref when an update is offered, else null. */
  available: string | null;
  /** Opens the What's new dialog; absent outside the shell. */
  onOpen?: () => void;
  releases?: readonly WhatsNewRelease[];
}) {
  const newest = newestRelease(releases);
  const intro = newest?.slides[0];
  const stable = channel !== "main";
  const notes = stable ? releaseNotesUrl(available) ?? releaseNotesUrl(current) : null;
  const notesFor = stable ? (releaseNotesUrl(available) ? available : current) : null;

  return (
    <section className={styles.box} aria-labelledby="update-whats-new-h" data-testid="update-whats-new">
      <h3 id="update-whats-new-h" className={styles.h}>
        {newest ? `What's new in ${releaseLabel(newest.version)}` : "What's new"}
      </h3>
      {intro ? (
        <>
          <p className={styles.title}>{intro.title}</p>
          {intro.tiles?.length ? (
            <ul className={styles.list}>
              {intro.tiles.map((t) => (
                <li key={t.slide}>
                  <b>{t.label}</b> — {t.text}
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.body}>{intro.body}</p>
          )}
        </>
      ) : (
        <p className={styles.body}>No release notes are bundled with this build.</p>
      )}
      <div className={styles.actions}>
        {newest && onOpen ? (
          <button type="button" className={styles.open} onClick={onOpen}>
            <Sparkles size={14} aria-hidden="true" /> Show what's new
          </button>
        ) : null}
        {notes ? (
          <a
            className={styles.link}
            href={notes}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="update-release-notes"
          >
            Release notes for {notesFor?.replace(/^v?/, "v")} on GitHub
            <ExternalLink size={13} aria-hidden="true" />
          </a>
        ) : null}
      </div>
      {!stable ? (
        <p className={styles.body} data-testid="update-main-notes">
          You're on <b>main</b>, the development branch: it moves commit by commit and has no
          release notes. Stable releases link to theirs here.
        </p>
      ) : null}
    </section>
  );
}
