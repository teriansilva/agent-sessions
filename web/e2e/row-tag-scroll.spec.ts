import { expect, test, type Page } from "@playwright/test";

// #551: real-browser proof for the custom per-session tag + the selected-row auto-scroll.
//   1. A tag renders (in the accent voice) BEFORE the AI summary on the row's second line.
//   2. "Set tag…" from the ⋯ menu opens an inline editor and the saved tag appears on the row.
//   3. The SELECTED row's overflowing title auto-scrolls (marquee) — but a non-selected row and
//      a reduced-motion viewport do not. Marquee assertions are desktop-only (the sidebar is a
//      grid column on desktop; off-canvas on mobile — same precedent as ai-review.spec.ts).
// Network is fully mocked so the rows are deterministic and no real backend/AI is needed.

const LONG_TITLE =
  "Refactor the entire pbkdf2 login verification path across every engine adapter and codepath, end to end";

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:tagged",
    engine: "claude",
    uuid: "tagged",
    short_uuid: "tagged",
    cwd: "/home/u/app",
    project: { kind: "folder", id: "/home/u/app", name: "app" },
    last_mtime: now,
    first_user_message: "",
    title: "Auth refactor",
    sticky: false,
    archived: false,
    working: false,
    tag: "🔥 hotpath",
    ai_summary: "Wiring the pbkdf2 check into the login path",
  },
  {
    id: "claude:plain",
    engine: "claude",
    uuid: "plain",
    short_uuid: "plain",
    cwd: "/home/u/site",
    project: { kind: "folder", id: "/home/u/site", name: "site" },
    last_mtime: now - 60,
    first_user_message: "",
    title: "Landing copy",
    sticky: false,
    archived: false,
    working: false,
    ai_summary: "Polishing the hero subhead wording",
  },
  {
    id: "claude:longrow",
    engine: "claude",
    uuid: "longrow",
    short_uuid: "longrow",
    cwd: "/home/u/x",
    project: { kind: "folder", id: "/home/u/x", name: "x" },
    last_mtime: now - 120,
    first_user_message: "",
    title: LONG_TITLE,
    sticky: false,
    archived: false,
    working: false,
  },
];

async function setup(page: Page, project: string): Promise<void> {
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
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  // Echo the posted tag back (trimmed like the server), driving the optimistic row update.
  await page.route("**/api/sessions/*/tag", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as { tag?: string };
    await r.fulfill({ json: { id: "claude:plain", tag: (body.tag ?? "").trim() } });
  });

  await page.goto("/");
  if (project === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

test.describe("custom row tag (#551)", () => {
  test("renders the tag before the AI summary on the row", async ({ page }, testInfo) => {
    await setup(page, testInfo.project.name);
    const row = page.getByRole("listitem").filter({ hasText: "Auth refactor" });
    // The tag leads the summary line, joined by " · " — proving it renders BEFORE the summary.
    await expect(row).toContainText("🔥 hotpath · Wiring the pbkdf2 check into the login path");
    // An untagged row shows only its summary (unchanged behaviour).
    const plain = page.getByRole("listitem").filter({ hasText: "Landing copy" });
    await expect(plain).toContainText("Polishing the hero subhead wording");
    await expect(plain).not.toContainText("🔥");
  });

  test("Set tag… from the ⋯ menu adds the tag to the row", async ({ page }, testInfo) => {
    await setup(page, testInfo.project.name);
    const plain = page.getByRole("listitem").filter({ hasText: "Landing copy" });
    await expect(plain).not.toContainText("review ·");

    await plain.hover();
    await plain.getByRole("button", { name: "Session actions" }).click();
    const menu = page.getByRole("menu", { name: "Session actions" });
    await menu.getByRole("menuitem", { name: "Set session tag" }).click();

    const input = page.getByRole("textbox", { name: "Session tag" });
    await expect(input).toBeVisible();
    await input.fill("review");
    await input.press("Enter");

    // The row (re-located after the edit form closes) now leads its summary with the tag.
    await expect(page.getByRole("listitem").filter({ hasText: "Landing copy" })).toContainText(
      "review · Polishing the hero subhead wording",
    );
  });
});

test.describe("selected-row auto-scroll (#551)", () => {
  test("the selected row's overflowing title scrolls; a non-selected one does not", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name === "mobile",
      "sidebar is off-canvas on mobile — desktop covers the row surface",
    );
    await setup(page, testInfo.project.name);

    // Not selected yet → the long title truncates statically (no animation).
    const idleTitle = page.getByRole("listitem").filter({ hasText: LONG_TITLE }).getByText(LONG_TITLE);
    await expect(idleTitle).toHaveCSS("animation-name", "none");

    // Select the row → its overflowing title now runs the ping-pong marquee animation.
    await page.goto("/s/claude/longrow");
    await expect(page.locator("aside.sidebar")).toBeVisible();
    const activeTitle = page.getByRole("listitem").filter({ hasText: LONG_TITLE }).getByText(LONG_TITLE);
    await expect(activeTitle).toHaveCSS("animation-name", /marq/);
  });

  test("reduced-motion keeps the selected row static (no marquee)", async ({ browser }) => {
    const context = await browser.newContext({ reducedMotion: "reduce" });
    const page = await context.newPage();
    await setup(page, "desktop");
    await page.goto("/s/claude/longrow");
    await expect(page.locator("aside.sidebar")).toBeVisible();
    const row = page.getByRole("listitem").filter({ hasText: LONG_TITLE });
    // Selected + overflowing, but reduced motion → the inner text span never animates.
    await expect(row.getByText(LONG_TITLE)).toHaveCSS("animation-name", "none");
    // The full text stays reachable via the native title tooltip on the line wrapper.
    await expect(row.getByTitle(LONG_TITLE).first()).toBeVisible();
    await context.close();
  });
});
