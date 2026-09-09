/** #935 — the mission route owns the shell.
 *
 *  Real-browser tests because every claim here is a GEOMETRY claim, and the defect they guard was
 *  invisible to the tests that already existed: `detail-column` was asserted *visible*, and it
 *  was — stacked under the thread at full width, in a console whose third grid track nothing had
 *  ever occupied. Visibility could not tell those apart. Boxes can.
 */
import { expect, test, type Page } from "@playwright/test";

import { missionList, missionRow, mockMissions } from "./mission-console";

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
  scan_depth: "medium",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: null,
  cards: [],
};

async function stub(page: Page, rows: unknown[] = []) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
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

test.describe("the shell's sidebar is the mission rail (#935)", () => {
  test("the rail renders inside the app shell, and the console keeps no rail of its own", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page);
    await page.goto("/pulse");
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
    await expect(page.getByRole("heading", { name: "Sessions" })).toBeAttached();

    await page.goto("/pulse");
    await page.getByTestId("mission-console").waitFor();
    await expect(page.getByRole("heading", { name: "Missions" })).toBeAttached();
    await expect(page.getByRole("heading", { name: "Sessions" })).toHaveCount(0);

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Sessions" })).toBeAttached();
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
      await page.goto("/pulse");
      await page.getByTestId("pane").waitFor();

      const con = await box(page, "mission-console");
      const pane = await box(page, "pane");
      await expect(page.getByTestId("detail-column")).toHaveCount(0);

      // RED before the fix: a 340px track sat to the right of the pane in every state, so the
      // pane stopped ~340px short of the console's own right edge.
      const shortfall = con.x + con.width - (pane.x + pane.width);
      expect(shortfall).toBeLessThan(24);
    });
  }

  test("a selected mission puts the detail BESIDE the thread, not under it", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1600, height: 900 });
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/pulse");
    await page.getByTestId("detail-column").waitFor();

    const pane = await box(page, "pane");
    const det = await box(page, "detail-column");

    // THE ACTUAL DEFECT. The aside was a flex child of a column-direction parent, so it stacked
    // BELOW the thread at full width while every existing test — which asked only whether it was
    // visible — went on passing. Same top edge, further right, narrower than the thread.
    expect(Math.abs(det.y - pane.y)).toBeLessThan(4);
    expect(det.x).toBeGreaterThan(pane.x + pane.width - 4);
    expect(det.width).toBeLessThan(pane.width);
  });

  test("below the wide breakpoint there is no detail column at all", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop shell");
    await page.setViewportSize({ width: 1399, height: 900 });
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/pulse");
    await page.getByTestId("pane").waitFor();
    // The stop strip owns objectives/timeline at this width; a column here would draw them twice.
    await expect(page.getByTestId("detail-column")).toBeHidden();
    await expect(page.getByTestId("stop-objectives")).toBeVisible();
  });
});

test.describe("mobile keeps the console's own drawer, on purpose (#935)", () => {
  test("the rail is not portalled where the shell sidebar is off-canvas", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "phone shell");
    await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
    await page.goto("/pulse");
    await page.getByTestId("mission-console").waitFor();

    // No slot: the shell's off-canvas panel has a backdrop but no `aria-modal` and no focus trap,
    // and `MissionDrawer` has both. The layout win is not worth that trade on a phone.
    await expect(page.locator("#mission-rail-slot")).toHaveCount(0);
    const trigger = page.getByTestId("rail-drawer-open");
    await expect(trigger).toBeVisible();

    await trigger.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveAttribute("aria-modal", "true");

    // Selecting closes it, and focus returns to the trigger — the contract that would have been
    // lost by routing the rail through the shell here.
    await dialog.getByRole("button", { name: /a mission/i }).first().click();
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });
});

/** The teardown contract (#935).
 *
 *  The issue is explicit that this does NOT promise a surviving socket: `/pulse` and
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
      const el = document.querySelector(sel) as (HTMLElement & Record<string, unknown>) | null;
      if (el) el["__probe935"] = tag;
    };
    mark(".app", "app");
    mark(".sidebar", "sidebar");
    mark(".hud-topbar", "topbar");
  });

  // CLIENT-SIDE navigation, via the control an operator actually uses. `page.goto` is a full
  // document load and would tear the DOM down whatever the code did — it could never distinguish
  // a remount from a reload, so it would have "failed" against a correct implementation.
  await page.getByRole("link", { name: /open mission control/i }).click();
  await page.getByTestId("mission-console").waitFor();
  await expect(page.locator("#mission-rail-slot")).toBeAttached();

  const survived = await page.evaluate(() => {
    const read = (sel: string) =>
      (document.querySelector(sel) as (HTMLElement & Record<string, unknown>) | null)?.[
        "__probe935"
      ] ?? null;
    return { app: read(".app"), sidebar: read(".sidebar"), topbar: read(".hud-topbar") };
  });

  // The shell, its sidebar and its topbar are the SAME elements — only what the sidebar renders
  // inside itself changed. RED if the swap were done by remounting the shell.
  expect(survived).toEqual({ app: "app", sidebar: "sidebar", topbar: "topbar" });

  // …and back out again, still the same shell.
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Sessions" })).toBeAttached();
  const stillThere = await page.evaluate(
    () =>
      (document.querySelector(".sidebar") as (HTMLElement & Record<string, unknown>) | null)?.[
        "__probe935"
      ] ?? null,
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
  await page.goto("/pulse");

  const create = page.getByTestId("rail-new-mission");
  await expect(create).toBeVisible();
  // In the sidebar, above the list — the same place its counterpart sits on the sessions side.
  const inSidebar = await create.evaluate((el) => !!el.closest(".sidebar"));
  expect(inSidebar).toBe(true);

  // It does not create anything on its own: it puts the composer into NEW MISSION and focuses
  // the field where the brief is written. Asserting the mode WITHOUT the focus would pass for a
  // button that leaves the operator hunting for the cursor.
  await create.click();
  await expect(page.getByTestId("composer-mode-new")).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByTestId("new-mission-instruction")).toBeFocused();
});

test("the archived scope offers no create, because the server refuses one (#935)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page);
  await page.goto("/pulse");
  await expect(page.getByTestId("rail-new-mission")).toBeVisible();

  await page.getByTestId("rail-scope").click();
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
    await page.goto("/pulse");
    await page.getByTestId("mission-console").waitFor();
    await page.locator('[data-testid="rail-mission"]').first().waitFor();

    const geom = await page.evaluate(() => {
      const body = document.querySelector(".sidebarBody") as HTMLElement;
      const slot = document.getElementById("mission-rail-slot") as HTMLElement;
      const rail = document.querySelector(
        'nav[aria-label="Missions" i]',
      ) as HTMLElement;
      const owner = [slot, rail].find((el) => el.scrollHeight > el.clientHeight);
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
    await page.locator("#mission-rail-slot").hover();
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
    await page.goto("/pulse");
    // A populated list auto-selects, so this is the ORDINARY state — and the one where the first
    // implementation did nothing at all: the console renders `MissionBody`, whose composer sends
    // messages to that mission and has no creation field. The empty-list test passed throughout.
    await page.getByTestId("detail-column").waitFor();

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

    await page.goto("/pulse");
    await page.getByTestId("mission-console").waitFor();

    // RED before the fix: only the "Order" LABEL was hidden, so Recent / Created still rendered
    // above the mission rail — and clicking Created wrote `session_list_order` to /api/prefs.
    // An active control for the wrong collection, not a stale word.
    await expect(page.getByRole("radio", { name: /^created$/i })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: /^recent$/i })).toHaveCount(0);
    expect(
      prefWrites.filter((w) =>
        JSON.stringify(w ?? {}).includes("session_list_order"),
      ),
    ).toEqual([]);

    // …and it is still there for the collection it belongs to.
    await page.goto("/");
    await expect(
      page.getByRole("radio", { name: /^created$/i }),
    ).toBeVisible();
  });
});

test("the mission sidebar has no empty header strip where the sort control was (#937)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop shell");
  await page.setViewportSize({ width: 1600, height: 900 });
  await stub(page, [missionRow({ id: "m1", title: "a mission" })]);
  await page.goto("/pulse");
  await page.getByTestId("rail-new-mission").waitFor();

  // Removing the session sort control left its 38px row rendered and empty, with a rule under
  // it — a blank strip above the rail, which is the "weird margins" complaint this whole line of
  // work started from. The box collapses; the sr-only landmark heading stays, because the
  // <aside> still needs an accessible name.
  const head = await page.evaluate(() => {
    const el = document.querySelector(".sidebar-head") as HTMLElement;
    return { h: el.getBoundingClientRect().height, name: el.textContent?.trim() };
  });
  expect(head.h).toBeLessThan(4);
  expect(head.name).toBe("Missions");

  // And the primary action starts at the very top of the sidebar body.
  const [slot, create] = [
    (await page.locator(".sidebarBody").boundingBox())!,
    (await page.getByTestId("rail-new-mission").boundingBox())!,
  ];
  expect(create.y - slot.y).toBeLessThan(24);
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

  await page.goto("/pulse");
  await page.getByTestId("detail-column").waitFor();

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
  await expect(page.getByTestId("detail-column")).toBeVisible();

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
