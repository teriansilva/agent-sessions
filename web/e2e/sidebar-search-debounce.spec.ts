import { expect, test, type Page } from "@playwright/test";

// #561 Phase 1: typing in the sidebar search box must debounce — a burst of keystrokes issues ONE
// `/api/sessions?q=…` request (for the final value), not one per key. Each request server-side is a
// full uncached disk scan, so one-per-key made search feel slow. Runs on both projects (the debounce
// lives in the useSessionsList hook, pointer-agnostic; the sidebar is a grid column on desktop, an
// off-canvas drawer on mobile). Real browser required: the bug is the request cadence of real
// keystroke events against a real controlled input, which jsdom fake-timer tests can't prove end to
// end. Red before (5 chars → 5 requests h/he/hel/hell/hello); green after (1 request → hello).

/** Every `q` value the server was actually asked for (non-empty only — the bootstrap/poll q="" is
 *  not a search request). */
async function setup(page: Page, project: string): Promise<string[]> {
  const qSeen: string[] = [];
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
  await page.route("**/api/sessions**", (r) => {
    const q = new URL(r.request().url()).searchParams.get("q");
    if (q) qSeen.push(q);
    return r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    });
  });
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));

  await page.goto("/");
  // Desktop renders the sidebar as a grid column; mobile hides it behind the drawer toggle.
  if (project === "mobile") {
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
  return qSeen;
}

test("a burst of search keystrokes issues a single /api/sessions request (#561)", async ({
  page,
}, testInfo) => {
  const qSeen = await setup(page, testInfo.project.name);
  const search = page.getByRole("searchbox", { name: "Search sessions" });
  await expect(search).toBeVisible();

  // Type five characters faster than the 250 ms debounce window.
  await search.pressSequentially("hello", { delay: 20 });

  // The burst collapses: the final request carries "hello", and far FEWER than 5 requests go out —
  // the old code fired one per keystroke (h/he/hel/hell/hello = 5). We assert a strict upper bound
  // below the keystroke count rather than exactly 1, so a CPU-starved runner that lets one gap slip
  // past the debounce can't flake this (the deterministic "exactly one" is pinned by the vitest
  // useSessionsList test); on the old code all 5 fire, so it stays red.
  await expect.poll(() => qSeen.at(-1) ?? "", { timeout: 3000 }).toBe("hello");
  await page.waitForTimeout(300); // let any trailing debounced request land before counting
  expect(qSeen.length).toBeLessThan(5);
  // The input itself stayed fully responsive (its value updates every keystroke, undebounced).
  await expect(search).toHaveValue("hello");
});
