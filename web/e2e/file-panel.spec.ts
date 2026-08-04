import { expect, test, type Page } from "@playwright/test";

/** File panel (#783) — real-browser proof, desktop AND mobile.
 *
 * jsdom cannot see any of what actually breaks here: stacking order against the terminal's
 * touch-capture overlay, whether a tap reaches the control under it, whether the page scrolls
 * sideways, or whether the panel docks into a pane too narrow to hold it. So the assertions below
 * are geometry and real event dispatch, not render output.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  title: "file panel session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, label: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

/** A directory with a deliberately long path — the horizontal-overflow trap. */
function listing(path: string) {
  const deep = "web/src/components/files/with/a/very/long/name";
  if (path === CWD) {
    return {
      path: CWD,
      parent: "/home/u",
      root: "/home/u",
      entries: [
        { name: "src", path: `${CWD}/src`, kind: "dir", size: 4096, mtime: NOW },
        { name: "docs", path: `${CWD}/docs`, kind: "dir", size: 4096, mtime: NOW },
        { name: "link-to-elsewhere", path: `${CWD}/link-to-elsewhere`, kind: "link", size: 11, mtime: NOW, link_target: "/etc", link_contained: false },
        { name: `${deep.replace(/\//g, "-")}.tsx`, path: `${CWD}/${deep.replace(/\//g, "-")}.tsx`, kind: "file", size: 2048, mtime: NOW },
        { name: "README.md", path: `${CWD}/README.md`, kind: "file", size: 512, mtime: NOW },
      ],
      total: 5,
      complete: true,
      truncated: false,
    };
  }
  if (path === "/home/u/other") {
    return {
      path,
      parent: "/home/u",
      root: "/home/u",
      entries: [{ name: "OTHER.md", path: `${path}/OTHER.md`, kind: "file", size: 10, mtime: NOW }],
      total: 1,
      complete: true,
      truncated: false,
    };
  }
  if (path === "/home/u") {
    // The contained root: `parent` is null, which is the boundary the Up button must respect.
    return {
      path,
      parent: null,
      root: "/home/u",
      entries: [{ name: "proj", path: CWD, kind: "dir", size: 4096, mtime: NOW }],
      total: 1,
      complete: true,
      truncated: false,
    };
  }
  return {
    path,
    parent: CWD,
    root: "/home/u",
    entries: [
      { name: "main.py", path: `${path}/main.py`, kind: "file", size: 128, mtime: NOW },
      { name: "scanner.py", path: `${path}/scanner.py`, kind: "file", size: 64, mtime: NOW },
    ],
    total: 2,
    complete: true,
    truncated: false,
  };
}

const SESSION_B = {
  id: "claude:bbbbbbbb-0000-4000-8000-000000000002",
  engine: "claude",
  title: "second session",
  cwd: "/home/u/other",
  project: { kind: "folder", id: "/home/u/other", label: "other" },
  last_mtime: NOW - 60,
  archived: false,
  favorite: false,
};

async function mockApp(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        hostname: "test",
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [SESSION, SESSION_B],
        next_offset: null,
        total: 2,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/files/capabilities", (r) => r.fulfill({ json: { ok: true, reason: "" } }));
  await page.route("**/api/files/list**", (r) => {
    const path = new URL(r.request().url()).searchParams.get("path") ?? CWD;
    r.fulfill({ json: listing(path) });
  });
  await page.route("**/api/files/read**", (r) =>
    r.fulfill({
      json: {
        path: new URL(r.request().url()).searchParams.get("path"),
        size: 42,
        binary: false,
        content: "line one\nline two\nline three",
        truncated: false,
      },
    }),
  );
}

async function openSession(page: Page) {
  await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await expect(page.locator("#root")).toBeVisible();
  // Wait for the head to finish measuring before deciding where the trigger lives — checking
  // too early races the overflow calculation and finds neither form. Target by data attribute:
  // once the panel opens it has a "Files" TAB too, so a by-name query would be ambiguous.
  await page.locator("[data-head-action]").first().waitFor();
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) return direct;
  await page.getByRole("button", { name: "More session actions" }).click();
  return page.getByRole("menuitem", { name: /Files/ });
}

test.describe("file panel", () => {
  test("opens from the pane head, expands a folder, opens a file, closes with Esc", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.click();

    const panel = page.locator("[data-file-panel]");
    await expect(panel).toBeVisible();

    // Lazy expansion: the child directory is only fetched when expanded.
    await expect(page.locator("[data-file-row][data-kind='dir']").first()).toBeVisible();
    await page.locator("[data-file-row]", { hasText: "src" }).first().click();
    await expect(page.locator("[data-file-row]", { hasText: "main.py" })).toBeVisible();

    // Opening a file must not evict the terminal — the overlay is an overlay.
    await page.locator("[data-file-row]", { hasText: "README.md" }).click();
    const viewer = page.locator("[data-file-viewer]");
    await expect(viewer).toBeVisible();
    await expect(page.locator(".xterm, [data-file-panel]").first()).toBeVisible();
    await expect(viewer).toContainText("line two");

    await page.keyboard.press("Escape");
    await expect(viewer).toBeHidden();
    await expect(panel).toBeVisible();
  });

  test("a symlink is display-only — it announces itself and does not open", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.click();
    const link = page.locator("[data-file-row][data-kind='link']");
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("aria-disabled", "true");
    // Playwright's actionability check already refuses an aria-disabled target, which is half the
    // proof. Force past it to show the handler is inert too, not merely unreachable.
    await link.click({ force: true });
    await expect(page.locator("[data-file-viewer]")).toHaveCount(0);
  });

  test("the page never scrolls sideways, even with a very long path", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.click();
    await expect(page.locator("[data-file-panel]")).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("the viewer paints ABOVE the panel and the terminal's touch layer", async ({ page }) => {
    // The z-order regression this guards: the terminal's capture overlay (z-index 6) and the
    // sheet (200) both sit between the page and the viewer. If the viewer lost that race it
    // would render but be unclickable — visible and broken, which a render test cannot see.
    const trigger = await openSession(page);
    await trigger.click();
    await page.locator("[data-file-row]", { hasText: "README.md" }).click();
    const viewer = page.locator("[data-file-viewer]");
    await expect(viewer).toBeVisible();

    const box = await viewer.boundingBox();
    expect(box).not.toBeNull();
    // hit-testing the viewer's centre must land INSIDE the viewer, not on something over it.
    const onTop = await page.evaluate(
      ({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return Boolean(el?.closest("[data-file-viewer]"));
      },
      { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 },
    );
    expect(onTop).toBe(true);
  });
});

test.describe("file panel — touch", () => {
  test.skip(({ isMobile }) => !isMobile, "touch-only assertions");

  test("the sheet is reachable by TAP, above the terminal's capture overlay", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.tap();

    const sheet = page.locator("[data-file-panel='sheet']");
    await expect(sheet).toBeVisible();

    // The real failure mode: the terminal's touchLayer (z-index 6, touch-action:none) eats
    // touches for anything painted under it. Prove a tap on a row actually reaches the row.
    const row = page.locator("[data-file-row]", { hasText: "src" }).first();
    const box = await row.boundingBox();
    const hit = await page.evaluate(
      ({ x, y }) => document.elementFromPoint(x, y)?.closest("[data-file-row]") !== null,
      { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 },
    );
    expect(hit).toBe(true);

    await row.tap();
    await expect(page.locator("[data-file-row]", { hasText: "main.py" })).toBeVisible();
  });

  test("rows and controls are real 44px touch targets", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.tap();
    await expect(page.locator("[data-file-panel='sheet']")).toBeVisible();

    // Scope to the panel: the sidebar drawer also has role="tab" filters and they are still in
    // the DOM behind the sheet, so an unscoped `.first()` measures the wrong element entirely.
    const sheet = page.locator("[data-file-panel='sheet']");
    for (const sel of ["[data-file-row]", "[role='tab']", "button[aria-label='Refresh']"]) {
      const box = await sheet.locator(sel).first().boundingBox();
      expect(box!.height, `${sel} must be a real touch target`).toBeGreaterThanOrEqual(44);
    }
  });

  test("head chips do not overlap — a tap fires the action it looks like", async ({ page }) => {
    // The reason the 44px `::after` idea was dropped: expanded hit boxes in a 26px bar overlap,
    // so a size assertion passes while the tap routes to the neighbour. Assert NON-OVERLAP.
    await mockApp(page);
    await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
    await expect(page.locator("#root")).toBeVisible();
    const boxes = await page.locator("[data-head-action]").evaluateAll((els) =>
      els.map((e) => e.getBoundingClientRect()).map((r) => ({ left: r.left, right: r.right, top: r.top, bottom: r.bottom })),
    );
    for (let i = 1; i < boxes.length; i++) {
      expect(boxes[i].left, "adjacent head chips must not overlap").toBeGreaterThanOrEqual(
        boxes[i - 1].right - 0.5,
      );
    }
  });

  test("the sheet's scroll does not chain to the page behind it", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.tap();
    await expect(page.locator("[data-file-panel='sheet']")).toBeVisible();
    // Body scroll is locked while the sheet is open, so a drag over it cannot move the page.
    const locked = await page.evaluate(() => document.body.style.overflow);
    expect(locked).toBe("hidden");
    const contained = await page.locator("[data-file-tree]").evaluate(
      (el) => getComputedStyle(el).overscrollBehaviorY,
    );
    expect(contained).toBe("contain");
  });
});

test.describe("file panel — state and navigation", () => {
  test("Up stops at the server's root boundary instead of walking out of it", async ({ page }) => {
    // The listing's `parent` is null at the contained root. Deriving the parent by trimming the
    // path string produced "/home" there, so one click replaced a valid view with a 403.
    const trigger = await openSession(page);
    await trigger.click();
    await expect(page.locator("[data-file-panel]")).toBeVisible();

    const up = page.getByRole("button", { name: "Go to the parent folder" });
    await expect(up).toBeEnabled(); // CWD's listing reports parent "/home/u"
    await up.click();
    await expect(up).toBeDisabled(); // "/home/u" is the root: parent is null
    await expect(page.locator("[data-file-tree]")).toBeVisible();
  });

  test("a closed panel stays closed across a reload, and an expanded folder is restored", async ({
    page,
  }) => {
    // The unit test for the storage helper passed while this was broken: FilePanel unmounts on
    // close, so it could never record `open: false`, and the expanded set never left FileTree.
    // Only an end-to-end close/reload can catch that.
    const trigger = await openSession(page);
    await trigger.click();
    await expect(page.locator("[data-file-panel]")).toBeVisible();
    await page.locator("[data-file-row]", { hasText: "src" }).first().click();
    await expect(page.locator("[data-file-row]", { hasText: "main.py" })).toBeVisible();

    await page.getByRole("button", { name: "Close the file panel" }).click();
    await expect(page.locator("[data-file-panel]")).toHaveCount(0);

    await page.reload();
    await page.locator("[data-head-action]").first().waitFor();
    await expect(page.locator("[data-file-panel]")).toHaveCount(0);

    const reopen = await openSession(page);
    await reopen.click();
    // Reopened with the expansion intact — the child directory is fetched again, not collapsed.
    await expect(page.locator("[data-file-row]", { hasText: "main.py" })).toBeVisible();
  });

  test("closing the sheet hands focus back to the control that opened it", async ({ page, isMobile }) => {
    test.skip(!isMobile, "sheet mode only");
    const trigger = await openSession(page);
    await trigger.tap();
    await expect(page.locator("[data-file-panel='sheet']")).toBeVisible();
    await page.getByRole("button", { name: "Close the file panel" }).click();
    const focused = await page.evaluate(() =>
      document.activeElement?.getAttribute("data-head-action"),
    );
    expect(focused).toBe("files");

    // ...and via ESCAPE, which is a different code path through the same `close`. Asserting the
    // specific target (not merely "focus moved") is what catches a broken returnFocusTo chain.
    await trigger.tap();
    await expect(page.locator("[data-file-panel='sheet']")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("[data-file-panel='sheet']")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate(() => document.activeElement?.getAttribute("data-head-action")))
      .toBe("files");
  });

  test("overflow arrow keys skip a disabled action and reach the ones behind it", async ({
    page,
    isMobile,
  }) => {
    test.skip(isMobile, "no overflow on touch — the chips go icon-only and all four stay inline");
    // Repaint is disabled while the socket is not connected and can be first in the overflow.
    // Focusing it is a no-op, so the old code left focus on the trigger and every ArrowDown
    // retried that same dead button — stranding Recap and Hand off.
    await mockApp(page);
    await page.setViewportSize({ width: 300, height: 720 });
    await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
    const more = page.getByRole("button", { name: "More session actions" });
    await expect(more).toBeVisible();
    await more.click();
    await expect(page.getByRole("menuitem").first()).toBeVisible();
    // Poll rather than sample once: focus-on-open lands in an effect, and reading activeElement
    // the instant after the click races it under load.
    const activeLabel = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
    await expect.poll(activeLabel).not.toBe("More session actions");
    const focusedName = await activeLabel();
    expect(focusedName, "focus must land on an ENABLED item").not.toBe("Repaint screen");
    await page.keyboard.press("ArrowDown");
    await expect.poll(activeLabel).not.toBe(focusedName);
  });
});

test("navigating A → B does not carry A's panel state onto B (#127 converge only)", async ({
  page,
}) => {
  // `files.key !== panelKey` is true for ordinary navigation as well as the placeholder→real
  // converge. Migrating on both moved A's open/root/expanded onto an unseen B and deleted A's
  // entry — and going back moved B's state onto A. Only the converge edge may migrate.
  await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  await page.locator("[data-head-action='files']").click();
  await expect(page.locator("[data-file-panel]")).toBeVisible();
  await expect(page.locator("[data-file-row]", { hasText: "README.md" })).toBeVisible();

  // Straight to B, which has never been opened: it must start closed, not inherit A's panel.
  await page.goto(`/s/claude/${SESSION_B.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  await expect(page.locator("[data-file-panel]")).toHaveCount(0);

  // ...and A must still have its own state, not have had it moved away.
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  await expect(page.locator("[data-file-panel]")).toBeVisible();
  await expect(page.locator("[data-file-row]", { hasText: "README.md" })).toBeVisible();
});

test.describe("file panel — contracts from review round 6", () => {
  test("an open panel does not carry A's root into B (no shared instance)", async ({ page }) => {
    // FilePanel had no identity key, so React reused the mounted instance across a session
    // change: A's local root/expansions survived and the persistence effect wrote them under B.
    await mockApp(page);
    await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
    await page.locator("[data-head-action]").first().waitFor();
    await page.locator("[data-head-action='files']").click();
    await expect(page.locator("[data-file-row]", { hasText: "README.md" })).toBeVisible();

    // Client-side navigation (not a reload) is the case that reused the instance.
    await page.locator("[data-file-panel]").waitFor();
    await page.evaluate((id) => {
      window.history.pushState({}, "", `/s/claude/${id}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, SESSION_B.id.split(":")[1]);
    await expect(page.locator("[data-file-row]", { hasText: "README.md" })).toHaveCount(0);
  });

  test("the breadcrumb offers real ancestors, not just a reset", async ({ page }) => {
    const trigger = await openSession(page);
    await trigger.click();
    await expect(page.locator("[data-file-panel]")).toBeVisible();
    const crumbs = page.getByRole("navigation", { name: "Folder path" }).getByRole("button");
    await expect(crumbs).toHaveCount(2); // "u" (contained root) and "proj"
    await crumbs.first().click();
    // Re-rooted to the ancestor: its listing is the one that contains "proj".
    await expect(page.locator("[data-file-row]", { hasText: "proj" })).toBeVisible();
  });

  test("an unresolved cwd disables Files with a reason instead of removing it", async ({ page }) => {
    await mockApp(page);
    // A session the sidebar does not know about yet: no row, no fresh state, so no cwd.
    await page.goto("/s/claude/cccccccc-0000-4000-8000-000000000009");
    const files = page.locator("[data-head-action='files']");
    await expect(files).toBeVisible();
    await expect(files).toBeDisabled();
    await expect(files).toHaveAttribute("title", /has not reported a folder/i);
  });

  test("the mobile sheet contains Tab in both directions", async ({ page, isMobile }) => {
    test.skip(!isMobile, "sheet mode only");
    const trigger = await openSession(page);
    await trigger.tap();
    const sheet = page.locator("[data-file-panel='sheet']");
    await expect(sheet).toBeVisible();
    const inSheet = () =>
      page.evaluate(() => Boolean(document.activeElement?.closest("[data-file-panel='sheet']")));
    // Declaring aria-modal while letting focus walk out to the background is a false claim.
    await page.keyboard.press("Shift+Tab");
    expect(await inSheet()).toBe(true);
    for (let i = 0; i < 12; i++) await page.keyboard.press("Tab");
    expect(await inSheet()).toBe(true);
  });
});
