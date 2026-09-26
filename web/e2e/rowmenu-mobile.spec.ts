import { expect, test } from "@playwright/test";
import { mockRoster } from "./roster";

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:aaa",
    engine: "claude",
    uuid: "aaa",
    short_uuid: "aaa",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now,
    first_user_message: "",
    title: "First session",
    sticky: false,
    archived: false,
    working: false,
  },
];

async function stubShell(page: import("@playwright/test").Page) {
    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: ["claude"],
          terminal_backend: "ws",
          auth_mode: "none",
          overview_expanded: [],
          projects_hidden: [],
        },
      }),
    );
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions,
          next_offset: null,
          total: sessions.length,
          facets: {
            projects: [
              { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
            ],
            engines: ["claude"],
          },
        },
      }),
    );
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) =>
      r.fulfill({ json: { version: "test" } }),
    );
    // The move dialog focuses its first option only once the list has LOADED, so an unstubbed
    // fetch leaves it in its spinner with focus still outside — which would make the Escape
    // assertion below vacuous rather than red.
    await page.route(/\/api\/projects(\?.*)?$/, (r) =>
      r.fulfill({
        json: {
          projects: [
            { id: "p1", name: "Default project", cwd: "/home/u/proj", archived: false },
          ],
        },
      }),
    );

}

test.describe("Mobile Context Menu", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(
      testInfo.project.name !== "mobile",
      "drawer behavior is mobile-specific",
    );
    await stubShell(page);
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/");
    await expect(page.locator("header .navToggle")).toBeVisible();
  });

  test("actions trigger is visible and clickable on mobile (390px)", async ({
    page,
  }) => {
    // Open the sidebar drawer.
    await page.locator("header .navToggle").click();

    // Wait for sidebar to be visible.
    const sidebar = page.locator("aside.sidebar");
    await expect(sidebar).toBeVisible();

    // Find the first session row.
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    // The "..." button (Session actions) should be visible even without hover.
    const actionsTrigger = firstRow.getByRole("button", {
      name: "Session actions",
    });

    // Check if it's visible.
    await expect(actionsTrigger).toBeVisible();

    // Check opacity (should be 1 on mobile).
    const opacity = await actionsTrigger.evaluate(
      (el) => window.getComputedStyle(el.parentElement!).opacity,
    );
    expect(opacity).toBe("1");

    // Click it.
    await actionsTrigger.click();

    // The menu (bottom sheet) should open.
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Check if it's at the bottom (bottom: 0).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) =>
        Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible and functional in the 640px-800px range", async ({
    page,
  }) => {
    // Set viewport to 700px.
    await page.setViewportSize({ width: 700, height: 800 });

    // Open the sidebar drawer.
    await page.locator("header .navToggle").click();

    // Find the first session row.
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    const actionsTrigger = firstRow.getByRole("button", {
      name: "Session actions",
    });
    await expect(actionsTrigger).toBeVisible();

    // Click it.
    await actionsTrigger.click();

    // The menu should open.
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // In this range (700px), it should now be a bottom sheet (as it is <= 800px).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) =>
        Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible and functional at 768px (iPad portrait)", async ({
    page,
  }) => {
    // Specifically requested by Hermes review.
    await page.setViewportSize({ width: 768, height: 1024 });

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await expect(firstRow).toBeVisible();

    const actionsTrigger = firstRow.getByRole("button", {
      name: "Session actions",
    });
    await expect(actionsTrigger).toBeVisible();

    await actionsTrigger.click();
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // At 768px it should be a bottom sheet now (as it is <= 800px).
    // The sheet is anchored to the visible bottom of the (dynamic) viewport — its lower
    // edge sits at the viewport floor rather than behind a mobile browser toolbar.
    const atViewportBottom = await menu.evaluate(
      (el) =>
        Math.round(el.getBoundingClientRect().bottom) === window.innerHeight,
    );
    expect(atViewportBottom).toBe(true);
  });

  test("actions trigger is visible on devices that might report hover support (tablet/hybrid)", async ({
    page,
  }) => {
    // Some browsers/devices (like iPad or Chrome with a mouse) might not match `hover: none`.
    // We want to ensure that if isMobile (<= 800px) is true, the actions are visible.
    await page.setViewportSize({ width: 800, height: 1000 });

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    const actionsTrigger = firstRow.getByRole("button", {
      name: "Session actions",
    });

    await expect(actionsTrigger).toBeVisible();
  });

  // Regression (#405 follow-up): the bottom sheet used to be `position: fixed; bottom: 0`
  // with no height cap and no internal scroll, so on a real phone its lower actions + Cancel
  // sat behind the browser's bottom toolbar (the visual ≠ layout viewport gap) and a tall
  // sheet overflowed off the top with no way to scroll back — "menu opens but is cut off".
  // The sheet now lives in a dynamic-viewport (100dvh) wrapper, is capped + scrollable, and
  // every action stays reachable. (Headless Chromium can't model the visual/layout split, so
  // we assert the structural guarantees that make the cut-off impossible.)
  test("bottom sheet is viewport-bounded, scrollable, and fully reachable", async ({
    page,
  }) => {
    // Configure AI review so the sheet carries its tallest item set (Review / Exclude /
    // Rename / Archive + Cancel) — the case most likely to overflow a short phone.
    await page.route("**/api/config", (r) =>
      r.fulfill({
        json: {
          csrf: "x",
          new_session_engines: ["claude"],
          terminal_backend: "ws",
          auth_mode: "none",
          overview_expanded: [],
          projects_hidden: [],
          ai_review: { configured: true },
        },
      }),
    );
    await page.reload();

    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await firstRow.getByRole("button", { name: "Session actions" }).click();

    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Bounded to the viewport + internally scrollable (was max-height:none / overflow:visible).
    const shape = await menu.evaluate((el) => {
      const cs = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        maxHeightSet: cs.maxHeight !== "none",
        overflowY: cs.overflowY,
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        fullWidth: Math.round(r.width) === window.innerWidth,
        vh: window.innerHeight,
      };
    });
    expect(shape.maxHeightSet).toBe(true);
    expect(shape.overflowY).toBe("auto");
    expect(shape.fullWidth).toBe(true);
    expect(shape.top).toBeGreaterThanOrEqual(0); // never overflows above the viewport
    expect(shape.bottom).toBe(shape.vh); // pinned to the visible floor

    // Every action AND Cancel are inside the viewport (reachable, not clipped).
    const cancel = menu.getByRole("button", { name: "Cancel" });
    for (const item of [...(await menu.getByRole("menuitem").all()), cancel]) {
      const within = await item.evaluate((el) => {
        const r = el.getBoundingClientRect();
        return r.top >= 0 && r.bottom <= window.innerHeight + 1;
      });
      expect(within).toBe(true);
    }

    // The wrapper above the sheet is click-through: a tap there reaches the scrim and closes.
    await page.touchscreen.tap(page.viewportSize()!.width / 2, 20);
    await expect(menu).toBeHidden();
  });

  // Regression: on a real phone, Chrome/Safari show/hide the URL bar on the very tap that
  // opens the sheet, firing `resize` (and `scroll`) events. The sheet used to bind those as
  // close triggers (they only make sense for the trigger-anchored desktop popover), so it
  // flickered shut the instant you pressed ⋯. The mobile sheet must survive a viewport
  // resize/scroll — it's pinned to the viewport, not the trigger.
  test("sheet survives a viewport resize/scroll (no URL-bar flicker)", async ({
    page,
  }) => {
    await page.locator("header .navToggle").click();
    const firstRow = page.locator("ul[aria-label] li").first();
    await firstRow.getByRole("button", { name: "Session actions" }).click();

    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();

    // Android URL-bar collapse ⇒ a window resize / scroll while the sheet is open.
    await page.evaluate(() => window.dispatchEvent(new Event("resize")));
    await page.evaluate(() => window.dispatchEvent(new Event("scroll")));
    await page.waitForTimeout(150);

    // Still open — the sheet does not dismiss itself on viewport chrome changes.
    await expect(menu).toBeVisible();
  });
});

/** #940 review 1 — the drawer must survive its own portalled menu.
 *
 *  Making the shell drawer modal added an outside-click dismissal, and the Session-actions menu
 *  portals to `document.body`. DOM containment therefore said a press on "Rename" was outside the
 *  panel, so the drawer closed on `mousedown` — before the item's click finished — and the rename
 *  editor mounted inside a panel already sliding off-screen.
 *
 *  This taps the ACTION, not just the sheet. The existing cases here establish that the menu opens
 *  and is reachable; none of them selects an inline-edit action and then asks whether its host is
 *  still on screen, which is exactly the gap the defect lived in.
 */
test.describe("the drawer survives its own portalled menu (#940)", () => {
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "drawer behavior is mobile-specific");
    await stubShell(page);
    await mockRoster(page); // the manifest-generated roster (#853 P4)
    await page.goto("/");
    await expect(page.locator("header .navToggle")).toBeVisible();
  });

  test("choosing Rename keeps the drawer open and the editor on screen", async ({
    page,
  }) => {
    await page.locator("header .navToggle").click();
    const sidebar = page.locator("aside.sidebar");
    await expect(sidebar).toBeVisible();

    await page.locator('button[aria-label="Session actions"]').first().click();
    const menu = page.getByRole("menu", { name: /session actions/i });
    await expect(menu).toBeVisible();

    await menu.getByRole("menuitem", { name: /^rename/i }).click();

    // THE DRAWER IS STILL OPEN — asserted on the MODAL STATE, which flips synchronously with the
    // open flag. Geometry cannot express this: the panel closes by sliding out over a transition,
    // so its box is still on-screen for a frame or two after the dismissal and an immediate
    // `x >= 0` passes against the very bug it is meant to catch. (It did, on the first draft.)
    await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
    // …and give the transition time to have run, so a late close cannot hide behind the assert.
    await page.waitForTimeout(400);
    await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
    const box = await sidebar.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);

    // …and the editor the operator actually asked for is reachable inside it.
    const editor = sidebar.getByRole("textbox", { name: /session title/i });
    await expect(editor).toBeVisible();
    const eb = await editor.boundingBox();
    expect(eb!.x).toBeGreaterThanOrEqual(0);
    expect(eb!.x + eb!.width).toBeLessThanOrEqual(
      page.viewportSize()!.width + 1,
    );
  });

  /** #940 review 2, finding 1 — the KEYBOARD half of the same portal problem.
   *
   *  `[data-modal-inside]` taught the drawer's OUTSIDE-CLICK check that a portalled child is
   *  still its business. It said nothing about Tab, and the drawer's focus trap has the same
   *  blind spot for the same reason: Session brief portals to `<body>`, so by DOM containment
   *  every control in it is "escaped", and the trap hauled focus back to the drawer's own Close
   *  while the brief stayed open. Measured at 412×900 against this head before the fix; on `main`
   *  before the drawer became modal, the same press reached "Review now" inside the brief.
   *
   *  The claim is ownership, not containment: while a nested modal holds focus, the drawer is not
   *  the topmost surface and owes the keyboard nothing.
   */
  test("Tab inside a portalled child dialog stays in the child, and Escape closes only it", async ({
    page,
  }) => {
    await page.locator("header .navToggle").click();
    await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);

    await page.locator('button[aria-label="Session actions"]').first().click();
    const menu = page.getByRole("menu", { name: /session actions/i });
    await expect(menu).toBeVisible();
    await menu.getByRole("menuitem", { name: /session brief/i }).click();

    const brief = page.getByRole("dialog", { name: /.+/ }).filter({
      has: page.getByRole("button", { name: /close session brief/i }),
    });
    await expect(brief).toHaveCount(1);
    await expect(
      page.getByRole("button", { name: /close session brief/i }),
    ).toBeFocused();

    // THE PRESS. Hermes' reproduction exactly: on this head before the fix it landed on the
    // DRAWER's Close while the brief stayed open; on `main` before the drawer became modal it
    // reached "Review now" inside the brief. Asserted by asking which SURFACE owns the active
    // element rather than naming the control, so the brief's own tab order can change.
    //
    // Six presses, which is more than the brief has controls — that is the point. The child's own
    // containment is what makes press four land back on Close instead of walking into the page,
    // and neither half alone produces this: without the drawer standing down focus is stolen on
    // press one, and without the child's trap it leaks on press two.
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Tab");
      const owner = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el) return "none";
        if (el.closest("aside.sidebar")) return "drawer";
        return el.closest('[role="dialog"]') ? "child-dialog" : "elsewhere";
      });
      expect(owner, `focus left the brief on press ${i + 1}`).toBe(
        "child-dialog",
      );
    }

    // ONE ESCAPE, ONE SURFACE. Both handlers are document-level, so before the fix a single press
    // closed the brief AND the drawer beneath it — the operator loses a surface they never asked
    // to leave.
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("button", { name: /close session brief/i }),
    ).toHaveCount(0);
    await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
  });

  /** #940 review 3 — THE SAME CLAIM, FOR EVERY CHILD, however it happens to be mounted.
   *
   *  The first version of the stand-down also required the child to be OUTSIDE the panel in the
   *  DOM, written from the assumption that all three of these portal to `<body>`. Two do.
   *  **Move to project renders directly in the sidebar row**, so for the one child that is a DOM
   *  descendant the guard said "not nested", the drawer took the Escape, and the dialog was left
   *  mounted inside a panel that had just become parked and inert — the operator loses the surface
   *  they were standing in and keeps the one they wanted to leave.
   *
   *  Which is why this is a table rather than one more case: where a child is mounted is a
   *  rendering detail, and any assertion that can tell the two apart is testing the wrong thing.
   *
   *  Since #948 Move to project portals to `<body>` too, like the other two. The sidebar is a
   *  containing block for `position: fixed`, so its backdrop had been trapped in the drawer. That
   *  mount change is exactly what this table was written to be indifferent to.
   */
  for (const child of [
    {
      name: "Session brief (portalled to <body>)",
      item: /session brief/i,
      close: /close session brief/i,
    },
    {
      name: "Hand off (portalled to <body>)",
      item: /hand off/i,
      close: /close hand ?off/i,
    },
    {
      name: "Move to project (portalled to <body>)",
      item: /move session to a project/i,
      close: /close move dialog/i,
    },
  ] as const) {
    test(`one Escape closes ${child.name} and leaves the drawer open`, async ({
      page,
    }) => {
      await page.locator("header .navToggle").click();
      await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);

      await page
        .locator('button[aria-label="Session actions"]')
        .first()
        .click();
      const menu = page.getByRole("menu", { name: /session actions/i });
      await expect(menu).toBeVisible();
      await menu.getByRole("menuitem", { name: child.item }).click();

      // The child is up. Counted rather than named, so this holds for the one with no close
      // button of its own.
      const dialogs = page.locator('[role="dialog"]');
      await expect(dialogs).toHaveCount(2);

      // Focus is inside it — the precondition the stand-down keys on, asserted rather than
      // assumed, because a child that never took focus would make the press below vacuous.
      //
      // POLLED, not read once: two of these move focus in an effect that waits on a fetch (Move
      // to project focuses its first option only after the list loads), so a single read taken
      // when the dialog first appears is a race against the child's own setup rather than a
      // statement about it.
      await expect
        .poll(
          () =>
            page.evaluate(() => {
              const d = document.activeElement?.closest('[role="dialog"]');
              return !!d && !d.matches("aside.sidebar");
            }),
          { message: "the child dialog never took focus" },
        )
        .toBe(true);

      await page.keyboard.press("Escape");

      // ONE surface closed, and it was the child. The drawer keeps its dialog role — which flips
      // synchronously, unlike its transform — and is not parked.
      await expect(dialogs).toHaveCount(1);
      await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
      await expect(page.locator("aside.sidebar[inert]")).toHaveCount(0);
      if (child.close) {
        await expect(page.getByRole("button", { name: child.close })).toHaveCount(0);
      }
    });
  }

  /** #940 review 4 — THE STATES THE TABLE ABOVE CANNOT REACH.
   *
   *  That test polls until focus is inside the child before pressing Escape, which is a sound
   *  precondition and a blind spot: `MoveToProjectModal` moves focus to its first option only
   *  after `GET /api/projects` RESOLVES. Hold the request, or fail it, and focus stays on the
   *  trigger outside the dialog for as long as the operator looks at the spinner or the error —
   *  so the precondition is never met, and the one case where the parent still took the Escape
   *  was the one case the test declined to enter.
   *
   *  These two carry no focus prerequisite deliberately. Ownership is established by MOUNTING,
   *  not by focus arriving, and that is exactly the claim under test.
   */
  for (const state of [
    {
      name: "while its request is still in flight",
      route: async (page: import("@playwright/test").Page, gate: Promise<void>) => {
        await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
          await gate; // never resolved: the dialog stays on its spinner for the whole test
          await r.fulfill({ json: { projects: [] } });
        });
      },
      settled: /loading projects/i,
    },
    {
      name: "after its request has FAILED",
      route: async (page: import("@playwright/test").Page) => {
        await page.route(/\/api\/projects(\?.*)?$/, (r) =>
          r.fulfill({ status: 500, json: { detail: "nope" } }),
        );
      },
      settled: /couldn.t load projects/i,
    },
  ] as const) {
    test(`one Escape closes Move to project ${state.name}, and leaves the drawer open`, async ({
      page,
    }) => {
      // Registered AFTER `stubShell`, so it wins: Playwright matches the most recent route first.
      await state.route(page, new Promise<void>(() => {}));

      await page.locator("header .navToggle").click();
      await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
      await page
        .locator('button[aria-label="Session actions"]')
        .first()
        .click();
      await page
        .getByRole("menu", { name: /session actions/i })
        .getByRole("menuitem", { name: /move session to a project/i })
        .click();

      // The dialog is up and in the state this case is about — asserted, so a fixture that
      // silently loaded would not pass as a spinner or an error.
      await expect(page.locator('[role="dialog"]')).toHaveCount(2);
      await expect(page.getByText(state.settled)).toBeVisible();

      // NO focus precondition, and that is the point. One press, from wherever focus is.
      await page.keyboard.press("Escape");

      await expect(page.locator('[role="dialog"]')).toHaveCount(1);
      await expect(page.locator('aside.sidebar[role="dialog"]')).toHaveCount(1);
      await expect(page.locator("aside.sidebar[inert]")).toHaveCount(0);
      // …and the drawer is still usable, not merely still labelled.
      const box = await page.locator("aside.sidebar").boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
    });

    /** #940 review 5 — THE FIRST KEY IS Shift+Tab, and it used to be the way out.
     *
     *  Fixing the Escape case introduced this one. The dialog started focusing its own
     *  `tabIndex={-1}` container on mount, which is inside the panel and absent from the tab
     *  cycle — so containment compared it against the first and last tabbable, matched neither,
     *  and left the press to the browser. The very first Shift+Tab landed on the sidebar's
     *  Archived tab with the dialog still open.
     *
     *  Backwards specifically, and as the FIRST key: forwards from the container happened to work
     *  (the browser's next stop was inside the dialog anyway), so a Tab-only test passes against
     *  the defect. The direction that leaves is the one that has to be asserted.
     */
    test(`the first Shift+Tab stays inside Move to project ${state.name}`, async ({
      page,
    }) => {
      await state.route(page, new Promise<void>(() => {}));

      await page.locator("header .navToggle").click();
      await page
        .locator('button[aria-label="Session actions"]')
        .first()
        .click();
      await page
        .getByRole("menu", { name: /session actions/i })
        .getByRole("menuitem", { name: /move session to a project/i })
        .click();
      await expect(page.locator('[role="dialog"]')).toHaveCount(2);
      await expect(page.getByText(state.settled)).toBeVisible();

      // Both directions, starting backwards, and no manual focus move first — the point is where
      // the dialog PUT focus, not where a test can move it.
      for (const key of ["Shift+Tab", "Shift+Tab", "Tab", "Tab"] as const) {
        await page.keyboard.press(key);
        const where = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null;
          if (!el) return "none";
          if (el.closest("aside.sidebar[role='dialog'] [role='dialog']"))
            return "child";
          const d = el.closest('[role="dialog"]');
          if (!d) return "outside";
          return d.matches("aside.sidebar") ? "drawer" : "child";
        });
        expect(where, `focus left the dialog on ${key}`).toBe("child");
      }

      // …and the child is still the only thing that closed when asked.
      await expect(page.locator('[role="dialog"]')).toHaveCount(2);
    });
  }
});
