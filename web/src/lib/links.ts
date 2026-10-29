/** Public URLs the app links out to — one constant each, imported everywhere they appear (#987).
 *  A second literal is how the What's new CTA, the Help menu and the setup wizard end up pointing
 *  at three different hosts after the next move. */

/** The documentation site's home. */
export const DOCS_HOME_URL = "https://docs.battlelabos.com/";

/** The shared-memory guide, with the table of which agents can reach it (#1441). */
export const MEMORY_GUIDE_URL = `${DOCS_HOME_URL}guide/memory#which-agents-can-reach-it`;

/** The public source repository (the GitHub mirror). */
export const SOURCE_URL = "https://github.com/teriansilva/agent-sessions";

/** A STABLE release's notes on GitHub (#1085), or `null` for anything that is not a plain
 *  `vX.Y.Z` / `X.Y.Z` release version — the main channel reports a commit, which has no release
 *  page, and nothing else is ever spliced into the URL. */
export function releaseNotesUrl(version: string | null | undefined): string | null {
  const m = /^v?(\d+\.\d+\.\d+)$/.exec((version ?? "").trim());
  return m ? `${SOURCE_URL}/releases/tag/v${m[1]}` : null;
}

/** The longest user agent written into a report. Real ones are well under this; a custom or
 *  extension-padded one is cut so the URL stays far below GitHub's limit (#1742). */
export const ISSUE_UA_MAX = 300;

/** The longest version written into a report. A release or `0.0.0+<sha>` is far shorter. */
export const ISSUE_VERSION_MAX = 64;

/** A new issue on the public repo's tracker, its body prefilled (#1742). The reporter fills the
 *  title, reads the whole body and submits it from their own GitHub account. The app holds no
 *  credential for the repo and posts no issue.
 *
 *  Opening the link DOES send the body to GitHub: it is in the URL the browser requests. So it
 *  carries only the version and the browser's user agent. A public tracker is the wrong place for
 *  anything about the install (its origin, the operator's name, paths, session ids, logs), so none
 *  of those are parameters here. */
export function newIssueUrl({
  version,
  userAgent,
}: {
  version: string | null | undefined;
  userAgent: string | null | undefined;
}): string {
  const clip = (v: string | null | undefined, max: number) => {
    const t = v?.trim() || "unknown";
    return t.length > max ? `${t.slice(0, max)}…` : t;
  };
  const body = [
    "### What happened",
    "",
    "",
    "### What you expected",
    "",
    "",
    "### Steps to reproduce",
    "",
    "1. ",
    "",
    "### Environment",
    "",
    `- BattleLab: ${clip(version, ISSUE_VERSION_MAX)}`,
    `- Browser: ${clip(userAgent, ISSUE_UA_MAX)}`,
    "",
  ].join("\n");
  return `${SOURCE_URL}/issues/new?${new URLSearchParams({ body })}`;
}
