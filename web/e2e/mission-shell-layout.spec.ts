/** #935 — the mission route owns the shell.
 *
 *  Real-browser tests because every claim here is a GEOMETRY claim, and the defect they guard was
 *  invisible to the tests that already existed: the detail column was asserted *visible*, and it
 *  was — stacked under the thread at full width, in a console whose third grid track nothing had
 *  ever occupied. Visibility could not tell those apart. Boxes can.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  flipMissionScope,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;
const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};
const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  cards: [],
};

async function stub(page: Page, rows: unknown[] = []) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: OVERVIEW }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await mockMissions(page, { missions: missionList(rows) });
}

/** A typed deferred. `let x: (() => void) | null = null` assigned inside a Promise executor is
 *  narrowed to `null` by control-flow analysis, so `x?.()` is `never` and does not compile — a
 *  definite-assignment assertion states the fact the executor guarantees instead.
 *
 *  A second copy of the one in `mission-console-layout.spec.ts`, deliberately for now: #938
 *  moves it to a shared e2e helper along with the twelve pre-existing sites that need it, and
 *  doing that here would drag an unrelated sweep into a layout PR. */
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const box = async (p: Page, sel: string) =>
  (await p.getByTestId(sel).boundingBox())!;

/** Select a mission from the rail (#948 P3). Nothing is auto-selected any more — the section opens
 *  on the new-mission page — so every test about a SELECTED mission picks one itself, waiting for
 *  the row rather than for a selection that will never be made for it. */
async function selectMission(page: Page, title: RegExp = /a mission/i) {
  await openMissionRail(page);
  await page
    .getByRole("navigation", { name: /missions/i })
    .getByRole("button", { name: title })
    .first()
    .click();
  await page.getByTestId("mission-state").waitFor();
}

test.describe("the shell's sidebar is the mission rail (#935)", () => {
  test("the rail renders inside the app shell, and the console keeps no rail of its own", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();

    // The rail is in the SHELL's sidebar, not in the console — asserted structurally, because a
    // rail that merely *looks* left-most would pass a coordinate check while still being the
    // console's own second list.
    const where = await page.evaluate(() => {
      const slot = document.getElementById("mission-rail-slot");
      const nav = document.querySelector('nav[aria-label="Missions" i]');
      const console_ = document.querySelector(
        '[data-testid="mission-console"]',
      );
      return {
        slotInSidebar: !!slot?.closest(".sidebar"),
        railInSlot: !!(nav && slot && slot.contains(nav)),
        railInsideConsole: !!(nav && console_ && console_.contains(nav)),
        consoleChildren: console_ ? console_.children.length : -1,
      };
    });
    expect(where.slotInSidebar).toBe(true);
    expect(where.railInSlot).toBe(true);
    expect(where.railInsideConsole).toBe(false);
    // One child: the centre. The rail track and the never-occupied third track are both gone.
    expect(where.consoleChildren).toBe(1);

    // And the sidebar says what it is now listing.
    await expect(
      page.getByRole("button", { name: /collapse mission list/i }),
    ).toBeVisible();
  });

  test("the session list is NOT in the sidebar here, and comes back when you leave", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page);

    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Sessions" }),
    ).toBeAttached();

    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    await expect(
      page.getByRole("heading", { name: "Missions" }),
    ).toBeAttached();
    await expect(page.getByRole("heading", { name: "Sessions" })).toHaveCount(
      0,
    );

    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Sessions" }),
    ).toBeAttached();
  });
});

test.describe("the thread fills the width it was given (#935)", () => {
  for (const width of [1400, 1600]) {
    test(`no mission at ${width}px: nothing is reserved for a pane that is not rendered`, async ({
      page,
    }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "desktop shell");
      await page.setViewportSize({ width, height: 900 });
      await stub(page);
      await page.goto("/mission");
      // With nothing selected the workspace IS the new-mission page (#948 P3) — there is no thread
      // pane to measure, so the landing is what must own the width.
      await page.getByTestId("mission-landing").waitFor();

      const con = await box(page, "mission-console");
      const landing = await box(page, "mission-landing");
      await expect(page.getByTestId("detail-column")).toHaveCount(0);
      await expect(page.getByTestId("mission-details")).toHaveCount(0);

      // RED before the fix: a 340px track sat to the right of the pane in every state, so the
      // pane stopped ~340px short of the console's own right edge.
      const shortfall = con.x + con.width - (landing.x + landing.width);
      expect(shortfall).toBeLessThan(24);
    });
  }

  test("a selected mission ALSO gets the whole width — no track beside the thread (#942)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    await selectMission(page);

    const con = await box(page, "mission-console");
    const pane = await box(page, "pane");

    // THIS TEST USED TO ASSERT THE COLUMN'S GEOMETRY, and #935 was right to: the aside was a flex
    // child of a column-direction parent, so it stacked BELOW the thread at full width while every
    // test that asked only whether it was VISIBLE went on passing. #942 deleted the column — four
    // unrelated panes in a 340px track, none of them big enough to be useful — so the claim
    // becomes the one the operator cares about: with a mission selected, the thread still owns the
    // full content width. The same measurement as the no-mission case above, which is the point:
    // there is one layout now, not two.
    const shortfall = con.x + con.width - (pane.x + pane.width);
    const details = (await page.getByTestId("mission-details").boundingBox())!;
    expect(shortfall - details.width).toBeLessThan(24);
    expect(details.x).toBeGreaterThanOrEqual(pane.x + pane.width);
  });

  test("below 1400 the details are ONE disclosure above the thread; at 1400 they sit beside it (#948)", async ({
    page,
  }, testInfo) => {
    // This was "the stop strip owns every pane": a Conversation / Details tab pair at every width.
    // #948 P3 removed the tabs. Below 1400px one `details-toggle` opens the details in a band
    // ABOVE the thread, and the thread stays on screen; at 1400px and above the toggle is gone and
    // the details are always beside the thread.
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1399, height: 900 });
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    await selectMission(page);
    const toggle = page.getByTestId("details-toggle");
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("mission-details")).toBeHidden();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("mission-details")).toBeVisible();
    await expect(page.getByTestId("detail-context")).toBeVisible();
    // The band is ABOVE the thread, and the thread did not go anywhere.
    await expect(page.getByTestId("pane")).toBeVisible();
    const details = await box(page, "mission-details");
    const pane = await box(page, "pane");
    expect(details.y + details.height).toBeLessThanOrEqual(pane.y + 1);

    // …and at 1400+ the disclosure is gone and the details are simply there, beside the thread.
    await page.setViewportSize({ width: 1600, height: 900 });
    await expect(toggle).toBeHidden();
    await expect(page.getByTestId("mission-details")).toBeVisible();
    await expect(page.getByTestId("detail-context")).toBeVisible();
    const wide = await box(page, "mission-details");
    const widePane = await box(page, "pane");
    expect(wide.x).toBeGreaterThanOrEqual(widePane.x + widePane.width);
  });
});

test.describe("the phone gets the same one rail, through the shell (#940)", () => {
  /** THE INVERSION. This asserted the opposite until #940: no slot on a phone, the console's own
   *  `☰` visible, and `MissionDrawer` supplying the dialog. That was the right call while the
   *  shell's off-canvas panel had a backdrop and none of the rest of the modal contract — trading
   *  a focus trap for a layout win is a bad bargain on the surface that most needs one.
   *
   *  The shell carries the contract itself now, so the trade is gone and with it the reason for a
   *  second drawer. The operator's report was exactly this: two hamburgers on one screen, one
   *  opening sessions and one opening missions. */
  test("one rail, opened by the shell's control, and it is a real modal", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();

    // The slot IS offered here now, and the console's own trigger is gone — not hidden, gone.
    await expect(page.locator("#mission-rail-slot")).toHaveCount(1);
    await expect(page.getByTestId("rail-drawer-open")).toHaveCount(0);

    const trigger = page.getByRole("button", { name: /Open mission list/i });
    await expect(trigger).toBeVisible();
    await trigger.click();

    // `aria-modal` is a PROMISE, so this checks the promise and not just the attribute: the panel
    // itself stays interactive while the background regions go inert.
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    await expect(page.locator("header.hud-topbar[inert]")).toHaveCount(1);
    await expect(page.locator("main.terminal-pane[inert]")).toHaveCount(1);
    await expect(page.locator("aside.sidebar[inert]")).toHaveCount(0);

    // Focus moved IN — onto the panel itself, because the hamburger that opened it is inside the
    // now-inert header and cannot be reached.
    await expect(page.locator("aside.sidebar")).toBeFocused();

    // Selecting closes it and returns focus to the trigger. Selection changes local state and
    // never the URL, so this only works because the shell hands the console a close callback.
    await dialog
      .getByRole("button", { name: /a mission/i })
      .first()
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator("header.hud-topbar[inert]")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test("Escape closes it and restores focus", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    const trigger = page.getByRole("button", { name: /Open mission list/i });
    await trigger.click();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  /** NO VISIBLE ✕, BUT AN ACCESSIBLE WAY OUT FROM INSIDE (#962). The ✕ #940 put in the head row
   *  floated mid-row on a phone and the operator asked for it gone. What has to survive is a dismiss
   *  INSIDE the modal: the opener is in the inert header and the scrim sits outside the dialog with
   *  tabIndex -1, so a touch screen-reader user restricted to the dialog would otherwise have none.
   *  Asserted with an EMPTY list, where choosing an item is not an exit, on both routes: nothing is
   *  visible, the in-dialog Close is announced, a keyboard sees it when it lands on it, activation
   *  closes and returns focus to the opener — and the scrim tap still works for pointers. */
  for (const [route, open] of [
    ["/mission", /Open mission list/i],
    ["/", /Open session list/i],
  ] as const) {
    test(`the ${route} drawer shows no ✕ yet keeps an accessible Close inside the dialog`, async ({
      page,
    }, testInfo) => {
      test.skip(testInfo.project.name !== "mobile", "phone shell");
      await stub(page, []);
      await page.goto(route);
      const trigger = page.getByRole("button", { name: open });
      const dialog = page.getByRole("dialog");
      const dismiss = dialog.getByRole("button", { name: /^close$/i });
      await expect(trigger).toBeVisible();

      // Hidden from the eye, present to assistive tech: one named control, a 1px sr-only clip.
      await trigger.click();
      await expect(dialog).toHaveCount(1);
      await expect(dialog.getByText("✕")).toHaveCount(0);
      await expect(dismiss).toHaveCount(1);
      const hidden = (await dismiss.boundingBox())!;
      expect(hidden.width * hidden.height).toBeLessThanOrEqual(1);

      // Activated the way assistive tech activates a button — a click event on the element, not a
      // coordinate tap — it closes and hands focus back to the opener.
      await dismiss.dispatchEvent("click");
      await expect(dialog).toHaveCount(0);
      await expect(trigger).toBeFocused();

      // A keyboard landing on it can see it, and Enter closes.
      await trigger.click();
      await expect(dialog).toHaveCount(1);
      await page.keyboard.press("Tab");
      await expect(dismiss).toBeFocused();
      const shown = (await dismiss.boundingBox())!;
      expect(shown.height).toBeGreaterThanOrEqual(44);
      expect(shown.width).toBeGreaterThanOrEqual(44);
      await page.keyboard.press("Enter");
      await expect(dialog).toHaveCount(0);
      await expect(trigger).toBeFocused();

      // The scrim covers the viewport under the panel; tap the strip the panel leaves uncovered.
      await trigger.click();
      await expect(dialog).toHaveCount(1);
      const vw = page.viewportSize()!.width;
      await page
        .getByRole("button", { name: /^Close (mission|session) list$/ })
        .click({ position: { x: vw - 12, y: 400 } });
      await expect(dialog).toHaveCount(0);
      await expect(trigger).toBeFocused();
    });
  }

  /** THE CONTROL. A docked column is not a dialog, and saying it is would tell a screen reader
   *  the rest of the page does not exist. Desktop must gain none of this. */
  test("the desktop column is NOT a dialog and nothing goes inert", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1400, height: 900 });
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    // The landing, since nothing is selected on arrival (#948 P3).
    await page.getByTestId("mission-landing").waitFor();

    await expect(page.locator("aside.sidebar")).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.locator("aside.sidebar[aria-modal]")).toHaveCount(0);
    await expect(page.locator("[inert]")).toHaveCount(0);
    await expect(page.getByTestId("drawer-close")).toHaveCount(0);
  });

  /** The drawer is app-wide, so the contract has to hold off this route too (#940). */
  test("a non-mission route's drawer is modal in the same way", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, []);
    await page.goto("/");
    const trigger = page.getByRole("button", { name: /Open session list/i });
    await expect(trigger).toBeVisible();
    await trigger.click();
    await expect(page.getByRole("dialog")).toHaveAttribute(
      "aria-modal",
      "true",
    );
    await expect(page.locator("main.terminal-pane[inert]")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  /** Resizing OUT of drawer mode while it is open must release the isolation — otherwise the
   *  operator lands on a desktop column with an inert page behind it. */
  test("crossing 800→801 with the drawer open releases inert", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.setViewportSize({ width: 800, height: 900 });
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    await page.getByRole("button", { name: /Open mission list/i }).click();
    await expect(page.locator("main.terminal-pane[inert]")).toHaveCount(1);

    await page.setViewportSize({ width: 801, height: 900 });
    await expect(page.locator("[inert]")).toHaveCount(0);
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  /** A PARKED DRAWER IS NOT A REACHABLE ONE (#940 review 2, finding 2).
   *
   *  The console's old `MissionDrawer` was UNMOUNTED when closed. The shell's `<aside>` is always
   *  in the DOM and merely translated out of the viewport — which hides it from the eye and from
   *  nothing else. Measured at 412×900 before the fix: tabbing forward from the header toggle put
   *  focus on a mission row whose right edge sat at −2px, with no dialog open. The operator gets
   *  no visible focus ring and can drive mission selection without ever revealing the control.
   *
   *  The claim is about REACHABILITY, not about geometry: a control that is off-screen AND
   *  unreachable is a parked drawer working correctly, which is why this asserts on where focus
   *  lands rather than on any box. */
  test("a CLOSED drawer keeps its controls out of the tab order", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    await page.waitForLoadState("networkidle");

    // Closed — no dialog, and the rail is parked off-canvas.
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const rail = page.getByRole("navigation", { name: /missions/i });
    const parked = await rail.boundingBox();
    expect(parked).not.toBeNull();
    expect(parked!.x + parked!.width).toBeLessThanOrEqual(1);

    // Walk forward from the shell's own toggle, well past the handful of controls the header and
    // the console offer, and never land inside the parked panel.
    await page.getByRole("button", { name: /Open mission list/i }).focus();
    for (let i = 0; i < 14; i++) {
      await page.keyboard.press("Tab");
      const landed = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el) return { inside: false, label: "none" };
        return {
          inside: !!el.closest("aside.sidebar"),
          label: (el.getAttribute("aria-label") || el.textContent || "")
            .trim()
            .slice(0, 40),
        };
      });
      expect(
        landed.inside,
        `press ${i + 1} focused "${landed.label}" inside the parked drawer`,
      ).toBe(false);
    }

    // …AND THE CONTROL: opening it puts them back. Without this the fix could be "make the
    // sidebar permanently unreachable on a phone", which passes the loop above and breaks the app.
    await page.getByRole("button", { name: /Open mission list/i }).click();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await expect(page.locator("aside.sidebar")).toBeFocused();
    await page.keyboard.press("Tab");
    const inside = await page.evaluate(
      () => !!document.activeElement?.closest("aside.sidebar"),
    );
    expect(inside, "an OPEN drawer must be reachable").toBe(true);
  });

  /** TAB IS CONTAINED, FORWARDS AND BACKWARDS (#940 review).
   *
   *  `inert` on the background is an attribute; containment is the behaviour it is supposed to
   *  buy, and only pressing the key establishes it. The test this replaces pressed Tab eight
   *  times against the console's own `MissionDrawer`; the shell owns the panel now, so the
   *  assertion moves here — and gains the reverse direction, because a trap that holds going
   *  forward and leaks on Shift+Tab is a trap that leaks. */
  test("Tab is contained inside the drawer, in both directions", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [
      missionRow({ id: "m1", title: "a mission" }),
      missionRow({ id: "m2", title: "another mission" }),
    ]);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    // The console's fetches are what cause the churn: the shell renders at once, then the rail
    // and stops re-render as the mission list and detail land — so an interaction issued in that
    // window resolves an element and then loses it ("detached from the DOM, retrying"). Waiting
    // for the network to go idle waits for exactly that window, and for nothing else.
    await page.waitForLoadState("networkidle");
    await page.getByRole("button", { name: /Open mission list/i }).click();
    await expect(page.getByRole("dialog")).toHaveCount(1);

    const inside = () =>
      page.evaluate(() => {
        const panel = document.querySelector('aside.sidebar[role="dialog"]');
        return !!panel && panel.contains(document.activeElement);
      });

    // Forwards, past the end of the panel's own controls so the WRAP is what is being tested and
    // not merely "there were still tab stops left".
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press("Tab");
      expect(await inside(), `focus escaped forwards on press ${i + 1}`).toBe(
        true,
      );
    }
    // …and backwards, past the start.
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press("Shift+Tab");
      expect(await inside(), `focus escaped backwards on press ${i + 1}`).toBe(
        true,
      );
    }
  });

  /** THE CONTRACT IS APP-WIDE, so it is asserted off `/` as well (#940 review).
   *
   *  `/` was the only non-mission route covered, and it is the one route whose pane is the new-
   *  session landing — the lightest content in the app. A session pane and the settings form are
   *  where the background actually has focusable content to leak into, which is what makes them
   *  the interesting cases rather than extra ones. */
  for (const [name, path] of [
    ["a session pane", "/s/claude/11111111-2222-3333-4444-555555555555"],
    ["settings", "/settings"],
  ] as const) {
    test(`the drawer is modal on ${name} too`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "mobile", "phone shell");
      await stub(page, []);
      await page.goto(path);

      const trigger = page.getByRole("button", { name: /Open session list/i });
      await expect(trigger).toBeVisible();
      await trigger.click();

      const dialog = page.getByRole("dialog");
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await expect(page.locator("header.hud-topbar[inert]")).toHaveCount(1);
      await expect(page.locator("main.terminal-pane[inert]")).toHaveCount(1);
      await expect(page.locator("aside.sidebar[inert]")).toHaveCount(0);
      await expect(page.locator("aside.sidebar")).toBeFocused();

      // The route's own content is behind the isolation, so Tab cannot reach it.
      for (let i = 0; i < 6; i++) {
        await page.keyboard.press("Tab");
        const inside = await page.evaluate(() => {
          const panel = document.querySelector('aside.sidebar[role="dialog"]');
          return !!panel && panel.contains(document.activeElement);
        });
        expect(inside, `focus escaped into ${path} on press ${i + 1}`).toBe(
          true,
        );
      }

      await page.keyboard.press("Escape");
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(trigger).toBeFocused();
    });
  }
});

/** The teardown contract (#935).
 *
 *  The issue is explicit that this does NOT promise a surviving socket: `/mission` and
 *  `/s/:engine/:id` are sibling routes, so navigating already unmounts `SessionView` and closes
 *  its terminal. That is today's behaviour and this change does not touch routing.
 *
 *  What the swap could plausibly break is the SHELL: if changing what the sidebar renders
 *  remounted the shell, everything it hosts would be torn down with it. So that is what is
 *  measured, directly — the identity of the shell's own DOM nodes across the round trip. A node
 *  that survives was not remounted, and a conditional render inside a stable tree is exactly the
 *  contract the issue commits to.
 *
 *  (An earlier version counted terminal sockets over the same trip. It was measuring the router,
 *  not the swap, and its failures were harness noise rather than signal — so it was replaced with
 *  the assertion that actually distinguishes the two.)
 */
test("navigating to the mission route does not remount the shell (#935)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);

  await page.goto("/");
  await page.locator(".sidebar").waitFor();

  // Brand the shell's long-lived nodes. A property on the element survives re-render and dies
  // with a remount, which is precisely the distinction under test.
  await page.evaluate(() => {
    const mark = (sel: string, tag: string) => {
      const el = document.querySelector(sel) as
        | (HTMLElement & Record<string, unknown>)
        | null;
      if (el) el["__probe935"] = tag;
    };
    mark(".app", "app");
    mark(".sidebar", "sidebar");
    mark(".hud-topbar", "topbar");
  });

  // CLIENT-SIDE navigation, via the control an operator actually uses. `page.goto` is a full
  // document load and would tear the DOM down whatever the code did — it could never distinguish
  // a remount from a reload, so it would have "failed" against a correct implementation.
  await page.getByRole("link", { name: "Missions", exact: true }).click();
  await page.getByTestId("mission-console").waitFor();
  await expect(page.locator("#mission-rail-slot")).toBeAttached();

  const survived = await page.evaluate(() => {
    const read = (sel: string) =>
      (
        document.querySelector(sel) as
          | (HTMLElement & Record<string, unknown>)
          | null
      )?.["__probe935"] ?? null;
    return {
      app: read(".app"),
      sidebar: read(".sidebar"),
      topbar: read(".hud-topbar"),
    };
  });

  // The shell, its sidebar and its topbar are the SAME elements — only what the sidebar renders
  // inside itself changed. RED if the swap were done by remounting the shell.
  expect(survived).toEqual({
    app: "app",
    sidebar: "sidebar",
    topbar: "topbar",
  });

  // …and back out again, still the same shell.
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Sessions" })).toBeAttached();
  const stillThere = await page.evaluate(
    () =>
      (
        document.querySelector(".sidebar") as
          | (HTMLElement & Record<string, unknown>)
          | null
      )?.["__probe935"] ?? null,
  );
  expect(stillThere).toBe("sidebar");
});

/** The rail is a SECTION, not just a list (#935) — the operator's words: "sessions and missions
 *  are separate sections. you have similar options as in the sessions sidebar, new mission etc."
 *  So the mission sidebar leads with its primary verb, exactly as the sessions sidebar leads
 *  with "+ New session". */
test("the mission sidebar leads with its own primary action (#935)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);
  await page.goto("/mission");

  const create = page.getByTestId("rail-new-mission");
  await expect(create).toBeVisible();
  // In the sidebar, above the list — the same place its counterpart sits on the sessions side.
  const inSidebar = await create.evaluate((el) => !!el.closest(".sidebar"));
  expect(inSidebar).toBe(true);

  // It does not create anything on its own: it returns to the landing — which IS the brief form
  // since #1058 removed the NEW MISSION | ASK strip — and focuses the field where the brief is
  // written. Asserting the form WITHOUT the focus would pass for a button that leaves the
  // operator hunting for the cursor.
  await create.click();
  await expect(page.getByTestId("new-mission-form")).toBeVisible();
  await expect(page.getByTestId("new-mission-instruction")).toBeFocused();
});

test("the archived scope offers no create, because the server refuses one (#935)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);
  await page.goto("/mission");
  await expect(page.getByTestId("rail-new-mission")).toBeVisible();

  await flipMissionScope(page);
  // Offering it here would advertise a mutation the backend answers 409 to.
  await expect(page.getByTestId("rail-new-mission")).toHaveCount(0);
});

/** #937 review 1 — three defects the first implementation shipped, all found in a browser and
 *  all invisible to the fixtures the original tests used. The first is the lesson: every case
 *  above uses zero or one mission, and a rail only fails to scroll once it has more content than
 *  the space it was given. A layout test with an empty list is barely a layout test.
 */
test.describe("#937 review 1 — the sidebar with a real list", () => {
  const manyRows = Array.from({ length: 50 }, (_, i) =>
    missionRow({ id: `m${i}`, title: `Mission number ${i}` }),
  );

  test("the rail SCROLLS inside the sidebar instead of growing past it (finding 1)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, manyRows);
    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();
    await page.locator('[data-testid="rail-mission"]').first().waitFor();

    const geom = await page.evaluate(() => {
      const body = document.querySelector(".sidebarBody") as HTMLElement;
      const slot = document.getElementById("mission-rail-slot") as HTMLElement;
      const rail = document.querySelector(
        'nav[aria-label="Missions" i]',
      ) as HTMLElement;
      const list = document.querySelector(
        '[data-testid="mission-list-scroll"]',
      ) as HTMLElement;
      const owner = [slot, rail, list].find(
        (el) => el.scrollHeight > el.clientHeight,
      );
      return {
        bodyH: body.clientHeight,
        slotH: slot.clientHeight,
        railH: rail.clientHeight,
        scrolls: !!owner,
      };
    });

    // RED before the fix: a 717px body containing a 2502px slot, `scrollHeight == clientHeight`
    // on every candidate, so nothing scrolled and the tail of the list was simply unreachable.
    expect(geom.slotH).toBeLessThanOrEqual(geom.bodyH + 1);
    expect(geom.railH).toBeLessThanOrEqual(geom.bodyH + 1);
    expect(geom.scrolls).toBe(true);

    // And it scrolls by WHEELING, which is how an operator reaches it. `scrollIntoView` would
    // pass against an unbounded container by moving the page instead of the list.
    const last = page.locator('[data-testid="rail-mission"]').last();
    const before = (await last.boundingBox())!.y;
    await page.getByTestId("mission-list-scroll").hover();
    await page.mouse.wheel(0, 4000);
    await expect
      .poll(async () => (await last.boundingBox())!.y)
      .toBeLessThan(before - 100);
    const box = (await last.boundingBox())!;
    expect(box.y).toBeLessThan(page.viewportSize()!.height);
  });

  test("+ New mission works with a mission already selected (finding 2)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, manyRows);
    await page.goto("/mission");
    // A mission SELECTED is the ordinary working state — and the one where the first
    // implementation did nothing at all: the console renders `MissionBody`, whose composer sends
    // messages to that mission and has no creation field. The empty-list test passed throughout.
    // Selected explicitly: since #948 P3 nothing is auto-selected on arrival.
    // Not anchored with `$`: a rail row's accessible name carries its meta line after the title.
    await selectMission(page, /Mission number 0(?!\d)/);

    await page.getByTestId("rail-new-mission").click();

    await expect(page.getByTestId("new-mission-instruction")).toBeVisible();
    await expect(page.getByTestId("new-mission-instruction")).toBeFocused();
  });

  test("the mission sidebar cannot re-sort the SESSION list (finding 3)", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, manyRows);

    const prefWrites: unknown[] = [];
    await page.route("**/api/prefs", async (r) => {
      if (r.request().method() !== "GET") {
        prefWrites.push(r.request().postDataJSON());
      }
      await r.fulfill({ json: {} });
    });

    await page.goto("/mission");
    await page.getByTestId("mission-console").waitFor();

    // RED before the fix: only the "Order" LABEL was hidden, so Recent / Created still rendered
    // above the mission rail — and clicking Created wrote `session_list_order` to /api/prefs.
    // An active control for the wrong collection, not a stale word.
    await expect(page.getByRole("radio", { name: /^created$/i })).toHaveCount(
      0,
    );
    await expect(page.getByRole("radio", { name: /^recent$/i })).toHaveCount(0);
    expect(
      prefWrites.filter((w) =>
        JSON.stringify(w ?? {}).includes("session_list_order"),
      ),
    ).toEqual([]);

    // …and it is still there for the collection it belongs to.
    await page.goto("/");
    await expect(page.getByRole("radio", { name: /^created$/i })).toBeVisible();
  });
});

test("the mission sidebar has no empty header strip where the sort control was (#937)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
  await page.goto("/mission");
  await page.getByTestId("rail-new-mission").waitFor();

  // THE HEAD ROW IS A ROW (#948 P2). Removing the session sort control once left its 38px row
  // rendered and EMPTY — a blank strip with a rule under it, the "weird margins" complaint — so
  // #937 collapsed it. #948 gives the mission route the same 38px head the sessions sidebar has,
  // carrying the MISSIONS tag and the rail's counts. What must never come back is the empty strip,
  // so the row has to hold visible content, not just height.
  const head = await page.evaluate(() => {
    const el = document.querySelector(".sidebar-head") as HTMLElement;
    return { h: el.getBoundingClientRect().height };
  });
  expect(head.h).toBeGreaterThanOrEqual(36);
  await expect(page.locator(".sidebar-head [data-testid='rail-counts']")).toBeVisible();
  await expect(page.locator(".sidebar-head [data-testid='rail-counts']")).toContainText(/\d+ active/);

  // And the primary action starts at the very top of the sidebar body.
  const [slot, create] = [
    (await page.locator(".sidebarBody").boundingBox())!,
    (await page.getByTestId("rail-new-mission").boundingBox())!,
  ];
  expect(create.y - slot.y).toBeLessThan(56);
});

/** #937 review 2 — a stale creation must not close a newer form.
 *
 *  Lifting the composer's mode to the console (review 1, finding 2) widened the lifetime of the
 *  "close the form" setter, and that turned a harmless no-op into data loss. Before the lift, a
 *  slow create settling after its composer unmounted wrote to a dead component. After it, the
 *  same late response reaches the console's SHARED mode and closes whatever form happens to be
 *  open — losing a draft the operator typed in between.
 *
 *  This is the "stale policy across the await" shape: a decision taken before a network call and
 *  applied after it, to a screen that has moved on.
 */
test("a slow creation settling later cannot close a newer form or eat its draft (#937)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page, [missionRow({ id: "m1", title: "an existing mission" })]);
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "infra" }] } }),
  );

  const gate = deferred();
  await page.route("**/api/missions", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    await gate.promise; // creation A hangs here while the operator moves on
    await r.fulfill({
      json: missionRow({ id: "mA", title: "created A" }),
    });
  });

  await page.goto("/mission");
  // Start from a selected mission, explicitly — nothing is auto-selected since #948 P3.
  await selectMission(page, /an existing mission/i);

  // A: start a creation and submit it, leaving the response in flight.
  await page.getByTestId("rail-new-mission").click();
  await page.getByTestId("new-mission-instruction").fill("mission A");
  const project = page.getByTestId("new-mission-project");
  if (await project.isVisible().catch(() => false)) {
    await project.selectOption({ index: 1 }).catch(() => {});
  }
  await page.getByTestId("new-mission-start").click();

  // Move to an existing mission — this unmounts the composer A was typed in.
  await page.locator('[data-testid="rail-mission"]').first().click();
  await expect(page.getByTestId("mission-state")).toBeVisible();

  // B: come back and start a second creation, with a draft in it.
  await page.getByTestId("rail-new-mission").click();
  const field = page.getByTestId("new-mission-instruction");
  await expect(field).toBeVisible();
  await field.fill("mission B, still being written");

  // A finally lands. RED before the fix: A's retained callback flipped the shared mode off, B's
  // form unmounted, and the draft went with it.
  gate.resolve();
  await page.waitForTimeout(600);

  await expect(page.getByTestId("new-mission-instruction")).toBeVisible();
  await expect(page.getByTestId("new-mission-instruction")).toHaveValue(
    "mission B, still being written",
  );
});
