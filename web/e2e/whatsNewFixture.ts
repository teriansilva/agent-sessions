import type { Page } from "@playwright/test";
import { FIXTURE_RELEASES } from "../src/whatsnew/fixtureRelease";

/** Hand the What's new test release (`src/whatsnew/fixtureRelease.ts`) to the page before its own
 *  scripts run. The app bundles no release notes (#2013). The unstamped preview build reads
 *  `window.__BATTLELAB_E2E_RELEASES__` instead (`bundledReleases`); a stamped one ignores it. */
export async function supplyWhatsNewFixture(page: Page): Promise<void> {
  await page.addInitScript((releases) => {
    (window as unknown as { __BATTLELAB_E2E_RELEASES__: unknown }).__BATTLELAB_E2E_RELEASES__ = releases;
  }, FIXTURE_RELEASES);
}
