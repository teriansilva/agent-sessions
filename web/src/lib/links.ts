/** Public URLs the app links out to — one constant each, imported everywhere they appear (#987).
 *  A second literal is how the What's new CTA, the Help menu and the setup wizard end up pointing
 *  at three different hosts after the next move. */

/** The documentation site's home. */
export const DOCS_HOME_URL = "https://docs.battlelabos.com/";

/** The public source repository (the GitHub mirror). */
export const SOURCE_URL = "https://github.com/teriansilva/agent-sessions";

/** A STABLE release's notes on GitHub (#1085), or `null` for anything that is not a plain
 *  `vX.Y.Z` / `X.Y.Z` release version — the main channel reports a commit, which has no release
 *  page, and nothing else is ever spliced into the URL. */
export function releaseNotesUrl(version: string | null | undefined): string | null {
  const m = /^v?(\d+\.\d+\.\d+)$/.exec((version ?? "").trim());
  return m ? `${SOURCE_URL}/releases/tag/v${m[1]}` : null;
}
