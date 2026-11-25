/** Artifacts… from a mission's ⋯ menu (#1969), in a real browser on desktop and phone.
 *
 *  The item used to open `MissionActivationDialog` synchronously. A phone's ⋯ menu is a modal
 *  bottom sheet that holds `inert` on `#root` and lifts it on a microtask after the click: the
 *  dialog mounted inside that window, saw an inert root that belonged to the sheet, and declined
 *  to mark it; the sheet then removed its mark, leaving an `aria-modal` over a live page. On
 *  desktop the panel simply stayed open behind the dialog.
 *
 *  The three goals from the issue, asserted after the sheet teardown has settled: `#root` is
 *  inert for as long as the dialog is open, focus is inside the dialog, and the menu is gone. */
import { expect, test } from "@playwright/test";

import { commonMocks } from "./mission-directions";
import { MISSION, missionList, missionRow, mockMissions } from "./mission-console";
import { mockRoster } from "./roster";

const MID = "msn_" + "a".repeat(32);

test("Artifacts… opens a dialog that owns the page and focus, and the menu is gone", async ({
  page,
}) => {
  await commonMocks(page);
  await mockRoster(page);
  await page.route("**/api/artifacts/activation**", (r) => {
    const url = new URL(r.request().url());
    return r.fulfill({
      json: {
        scope: url.searchParams.get("scope"),
        id: url.searchParams.get("id"),
        mode: null,
        enabled: true,
      },
    });
  });
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: MID, title: "Invoices, October", state: "running" }),
    ]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Invoices, October",
      state: "running",
      events: [],
      events_next_seq: null,
    },
  });
  await page.goto(`/mission?m=${MID}`);
  await page.getByTestId("mission-overflow").click();
  await page.getByTestId("mission-artifacts").click();
  const dialog = page.getByTestId("artifact-activation-mission");
  await expect(dialog).toBeVisible();

  // The page behind the dialog is inert — on a phone too, after its menu sheet lifted its own
  // mark — and it stays that way past the sheet's restore frame.
  await expect
    .poll(() =>
      page.evaluate(() => document.getElementById("root")?.hasAttribute("inert")),
    )
    .toBe(true);
  await page.waitForTimeout(500); // past any closing sheet's restore frame
  expect(
    await page.evaluate(() =>
      document.getElementById("root")?.hasAttribute("inert"),
    ),
  ).toBe(true);

  // Focus is INSIDE the dialog, never left on the ⋯ trigger behind the inert page (phone too).
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          !!document.activeElement?.closest(
            "[data-testid='artifact-activation-mission']",
          ),
      ),
    )
    .toBe(true);

  // The ⋯ menu is gone: on desktop it was closed through its trigger; on a phone its sheet closed.
  await expect(page.getByTestId("mission-overflow-menu")).toHaveCount(0);

  // Closing returns focus to the ⋯ trigger and lifts the page's inert mark.
  await page.getByRole("button", { name: "Close", exact: true }).first().click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.activeElement?.getAttribute("data-testid"))).toBe("mission-overflow");
  await expect
    .poll(() =>
      page.evaluate(() => document.getElementById("root")?.hasAttribute("inert")),
    )
    .toBe(false);
});
