/** What's new (#971): one entry per release, and the newest one wins. The dialog, the gate, the Help
 *  menu item and the Settings → Updates card all read this list. Each of them hides, or says "no
 *  release notes", when the list is empty.
 *
 *  **It ships empty (#2013).** The hand-written 0.20 slides went out of date before 0.20 was cut,
 *  so they were removed. Release notes are going to be generated from the merged work and delivered
 *  as data (#2013 Phase 4), not written into the bundle by hand. Until then, nothing auto-shows. */

export interface WhatsNewCta {
  label: string;
  /** An in-app route: navigates, then counts as dismissed. */
  to?: string;
  /** An external page: opens in a new tab, then counts as dismissed. */
  href?: string;
}

export interface WhatsNewSlide {
  id: string;
  eyebrow: string;
  title: string;
  body: string;
  points?: string[];
  items?: { title: string; text: string }[];
  /** Intro tiles that jump to another slide by id. */
  tiles?: { label: string; text: string; slide: string }[];
  /** Base name under the release's `assetDir`; `<image>-still.svg` is the paused variant. */
  image?: string;
  cta?: WhatsNewCta;
  /** Label for this slide's primary button (default "Next", or "Let's go" on the last slide). */
  primary?: string;
}

export interface WhatsNewRelease {
  /** Exactly MAJOR.MINOR.PATCH — it is what gets stored as `whats_new_seen`. */
  version: string;
  /** Under `web/public`. */
  assetDir: string;
  slides: WhatsNewSlide[];
}

/** The releases this build ships. */
export const RELEASES: readonly WhatsNewRelease[] = [];

/** The releases the app reads: `RELEASES`, or a list a real-browser spec supplies.
 *
 *  The installer always stamps a shipped bundle (`AGENT_SESSIONS_VERSION`), and a stamped bundle
 *  returns `RELEASES` as is. An unstamped build (vitest, the Playwright preview, pr-visual) also
 *  reads `window.__BATTLELAB_E2E_RELEASES__`, so the e2e specs can keep testing the dialog while no
 *  release is bundled. This is the same rule as `whatsNewBundleVersion`'s override, and it has no
 *  effect on an installed bundle. The list is checked loosely: an array of objects, each with a
 *  `version` and `slides`. Anything else falls back to `RELEASES`. */
export function bundledReleases(stamp: string = __APP_VERSION__): readonly WhatsNewRelease[] {
  if (stamp !== "dev") return RELEASES;
  const override = (globalThis as { __BATTLELAB_E2E_RELEASES__?: unknown }).__BATTLELAB_E2E_RELEASES__;
  if (!Array.isArray(override)) return RELEASES;
  const ok = override.every((r: unknown) => {
    if (!r || typeof r !== "object") return false;
    const { version, slides } = r as { version?: unknown; slides?: unknown };
    return typeof version === "string" && Array.isArray(slides);
  });
  return ok ? (override as WhatsNewRelease[]) : RELEASES;
}
