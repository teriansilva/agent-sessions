/** The action gallery (#2248 P1) for the browser tests: `GET /api/actions` answered with the
 *  server's own output (`fixtures/actions-gallery.json`, pinned by `tests/test_actions.py`), so a
 *  spec can never pass against a shape or wording the server does not send. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Page } from "@playwright/test";

import { ACTIONS_API } from "../src/lib/apiPaths";
import type { ActionGallery } from "../src/types/actions";

export const ACTION_GALLERY: ActionGallery = JSON.parse(
  readFileSync(resolve(process.cwd(), "e2e/fixtures/actions-gallery.json"), "utf8"),
);

/** Answer `GET /api/actions`; `gallery` changes what it says (a requirement not met, a failure). */
export async function mockActions(
  page: Page,
  gallery: ActionGallery | { status: number; detail: string } = ACTION_GALLERY,
): Promise<{ reads: () => number }> {
  let reads = 0;
  await page.route(`**${ACTIONS_API}`, (r) => {
    reads++;
    return "status" in gallery
      ? r.fulfill({ status: gallery.status, json: { detail: gallery.detail } })
      : r.fulfill({ json: gallery });
  });
  return { reads: () => reads };
}
