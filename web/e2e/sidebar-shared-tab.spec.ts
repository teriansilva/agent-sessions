import { expect, test, type Locator, type Page } from "@playwright/test";
import { MIN_W, WIDTH_KEY } from "../src/app/sidebarWidth";
import {
  GONE,
  GPU,
  GRANT_A,
  GRANT_B,
  GRANT_OFF,
  LAPTOP,
  URL_PATH,
  isMobile,
  selectScope,
  setup,
  showList,
  showShared,
  type FakeShare,
} from "./shareFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// The session list's Shared tab (#2198): sessions a paired instance shares with this one have
// their own scope beside Active and Archived, and no longer follow the local rows at the foot of
// the list. Mocked API (`shareFixture`): two local sessions; by default three shared ones, two by
// gpu-box and one by laptop (offline).

const tabs = (list: Locator) => list.getByRole("tablist", { name: "Which sessions" }).getByRole("tab");
const tab = (list: Locator, scope: "active" | "archived" | "shared") => list.getByTestId(`scope-tab-${scope}`);
const remote = (list: Locator) => list.getByTestId("remote-row");
const local = (list: Locator) => list.locator(`a[href^="/s/"]`);
const titles = (rows: Locator) =>
  rows.evaluateAll((els) => els.map((el) => el.querySelector("a span span")?.textContent ?? ""));

async function open(page: Page, prepare?: (fake: FakeShare) => void) {
  const fake = await setup(page, prepare);
  await page.goto(URL_PATH);
  const list = await showList(page);
  await expect(local(list)).toHaveCount(2);
  return { fake, list };
}

test("no third tab while nothing is shared", async ({ page }) => {
  const { fake, list } = await open(page, (f) => {
    f.remote = [];
  });
  await expect.poll(() => fake.listReads).toBeGreaterThanOrEqual(1);
  await expect(tabs(list)).toHaveText(["Active", "Archived"]);
  await expect(tab(list, "shared")).toHaveCount(0);
});

test("with a share the Shared tab is there, with the number of shared sessions", async ({ page }) => {
  const { list } = await open(page);
  await expect(tabs(list)).toHaveText(["Active", "Archived", "Shared 3"]);
  await expect(tab(list, "shared")).toHaveAttribute("aria-selected", "false");
  await expect(list.getByTestId("shared-count")).toHaveText("3");
});

test("Shared lists exactly the shared rows; Active and Archived list none of them", async ({ page }) => {
  const { fake, list } = await open(page);
  // Active: this instance's own sessions, and no shared row after them.
  await expect(local(list)).toHaveCount(2);
  await expect(remote(list)).toHaveCount(0);
  await selectScope(list, "archived");
  await expect(local(list)).toHaveCount(0);
  await expect(remote(list)).toHaveCount(0);

  await selectScope(list, "shared");
  await expect(remote(list)).toHaveCount(3);
  expect(await remote(list).locator("a").evaluateAll((els) => els.map((a) => a.getAttribute("href")))).toEqual([
    `/remote/${GPU}/${GRANT_A}`,
    `/remote/${GPU}/${GRANT_B}`,
    `/remote/${LAPTOP}/${GRANT_OFF}`,
  ]);
  await expect(list.getByRole("list", { name: "3 sessions shared by peers" })).toBeVisible();
  // Nothing of the local list is there: no row, no row action, no paging, no local filter.
  await expect(local(list)).toHaveCount(0);
  await expect(list.getByRole("button", { name: /^Session actions/ })).toHaveCount(0);
  await expect(list.getByRole("button", { name: "Load more" })).toHaveCount(0);
  await expect(list.getByRole("searchbox", { name: "Search sessions" })).toHaveCount(0);
  await expect(list.getByLabel("Filter by agent")).toHaveCount(0);
  await expect(list.getByTestId("filters-toggle")).toContainText("Shared");
  // The scope is the browser's: the server was asked about Active and Archived, never "shared".
  for (const q of fake.sessionQueries) expect(q).not.toMatch(/shared/i);
});

test("search narrows the shared rows by title and by peer name; Clear brings them back", async ({ page }) => {
  const { fake, list } = await open(page);
  await selectScope(list, "shared");
  const search = list.getByRole("searchbox", { name: "Search shared sessions" });
  const asked = fake.sessionQueries.length;
  await search.fill("PIPELINE"); // a title, whatever the case
  await expect(remote(list)).toHaveCount(1);
  expect(await titles(remote(list))).toEqual(["Color pipeline notes"]);
  await search.fill("gpu-box"); // a peer name
  await expect(remote(list)).toHaveCount(2);
  expect(await titles(remote(list))).toEqual(["Tune the render queue", "Color pipeline notes"]);
  await search.fill("Laptop");
  await expect(remote(list)).toHaveCount(1);
  expect(await titles(remote(list))).toEqual(["Field recording index"]);
  // The count is what is shared, not what matches.
  await expect(list.getByTestId("shared-count")).toHaveText("3");

  await search.fill("xyz");
  await expect(remote(list)).toHaveCount(0);
  await expect(list.getByTestId("shared-empty")).toHaveText(
    /^No shared session matches\.\s*Clear the search to see all 3\.$/,
  );
  await list.getByRole("button", { name: "Clear search" }).click();
  await expect(remote(list)).toHaveCount(3);
  await expect(tab(list, "shared")).toHaveAttribute("aria-selected", "true");
  await expect(search).toHaveValue("");
  // The search is the browser's: it asked the session API nothing.
  await page.waitForTimeout(600);
  expect(fake.sessionQueries.slice(asked).filter((q) => /pipeline|gpu|laptop|xyz/i.test(q))).toEqual([]);
  // ...and it is not the local search: Active is not narrowed by it, and has its own box.
  await selectScope(list, "active");
  await expect(local(list)).toHaveCount(2);
  await expect(list.getByRole("searchbox", { name: "Search sessions" })).toHaveValue("");
});

test("a shared row opens the read-only session", async ({ page }) => {
  await open(page);
  const list = await showShared(page);
  await remote(list).first().locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_A}$`));
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-title")).toHaveText("Tune the render queue");
  await expect(pane.getByTestId("remote-banner")).toContainText("Shared by gpu-box · view only");
  // Back in the list the tab is still Shared, and the open session's row is the current one.
  const again = await showList(page);
  await expect(tab(again, "shared")).toHaveAttribute("aria-selected", "true");
  await expect(remote(again).first().locator("a")).toHaveAttribute("aria-current", "page");
});

test("three tabs in equal thirds that fit, a two-digit count too; 44 px on a phone", async ({ page }) => {
  // The narrowest sidebar an operator can drag to (desktop); the drawer on a phone.
  await page.addInitScript(([k, w]) => localStorage.setItem(k, w), [WIDTH_KEY, String(MIN_W)]);
  const { list } = await open(page, (f) => {
    f.remote = Array.from({ length: 12 }, (_, i) =>
      f.row(GPU, "gpu-box", `s${String(i).padStart(2, "0")}aBcDeFgHiJkLmNoPqRs`, `Shared session ${i + 1}`, true),
    );
  });
  if (!isMobile(page)) expect((await list.boundingBox())!.width).toBeLessThanOrEqual(MIN_W + 1);
  for (const scope of ["active", "shared"] as const) {
    await selectScope(list, scope);
    await expect(tabs(list)).toHaveText(["Active", "Archived", "Shared 12"]);
    const boxes = await tabs(list).evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect();
        return { w: r.width, h: r.height, clipped: el.scrollWidth > el.clientWidth + 1 };
      }),
    );
    // Equal thirds (the hairline between two cells is the only difference)...
    for (const b of boxes) expect(Math.abs(b.w - boxes[0].w), JSON.stringify(boxes)).toBeLessThanOrEqual(1.5);
    // ...and every label, the count included, fits its cell.
    expect(boxes.map((b) => b.clipped)).toEqual([false, false, false]);
    if (isMobile(page)) for (const b of boxes) expect(b.h).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    const bar = (await list.getByRole("tablist", { name: "Which sessions" }).boundingBox())!;
    const side = (await list.boundingBox())!;
    expect(bar.x).toBeGreaterThanOrEqual(side.x);
    expect(bar.x + bar.width).toBeLessThanOrEqual(side.x + side.width + 0.5);
  }
});

test("the keyboard moves across the three tabs: one tab stop, arrows, Home and End", async ({ page }) => {
  const { list } = await open(page);
  const selected = () =>
    tabs(list).evaluateAll((els) => els.map((el) => `${el.getAttribute("aria-selected")}/${(el as HTMLElement).tabIndex}`));
  expect(await selected()).toEqual(["true/0", "false/-1", "false/-1"]);
  await tab(list, "active").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab(list, "archived")).toBeFocused();
  await expect(tab(list, "archived")).toHaveAttribute("aria-selected", "true");
  await expect(local(list)).toHaveCount(0);
  await page.keyboard.press("ArrowRight");
  await expect(tab(list, "shared")).toBeFocused();
  await expect(tab(list, "shared")).toHaveAttribute("aria-selected", "true");
  await expect(remote(list)).toHaveCount(3);
  expect(await selected()).toEqual(["false/-1", "false/-1", "true/0"]);
  await page.keyboard.press("ArrowRight"); // wraps
  await expect(tab(list, "active")).toBeFocused();
  await expect(local(list)).toHaveCount(2);
  await page.keyboard.press("End");
  await expect(tab(list, "shared")).toBeFocused();
  await expect(remote(list)).toHaveCount(3);
  await page.keyboard.press("ArrowLeft");
  await expect(tab(list, "archived")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tab(list, "active")).toBeFocused();
  await expect(tab(list, "active")).toHaveAttribute("aria-selected", "true");
});

test("the last share ends on the tab: it stays, and focus goes to the selected tab when it leaves", async ({
  page,
}) => {
  let ended = false;
  const { fake, list } = await open(page, (f) => {
    f.remote = [f.row(GPU, "gpu-box", GRANT_A, "Tune the render queue", true)];
    const live = f.onRead;
    f.onRead = (...a) => (ended ? GONE : live(...a));
  });
  await selectScope(list, "shared");
  await expect(tabs(list)).toHaveText(["Active", "Archived", "Shared 1"]);
  await remote(list).first().locator("a").click();
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-title")).toHaveText("Tune the render queue");
  // The owner ends the share. The pane says so and asks for the list again: it is empty now.
  fake.remote = [];
  ended = true;
  await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
  const side = await showList(page);
  // The tab the operator is on stays, without a count, and says what is the case.
  await expect(remote(side)).toHaveCount(0);
  await expect(tabs(side)).toHaveText(["Active", "Archived", "Shared"]);
  await expect(tab(side, "shared")).toHaveAttribute("aria-selected", "true");
  await expect(side.getByTestId("shared-empty")).toContainText("Nothing is shared with you right now.");
  await expect(local(side)).toHaveCount(0);
  // Leaving it by keyboard: the tab goes, and focus is on the tab now selected.
  await tab(side, "shared").focus();
  await page.keyboard.press("ArrowLeft");
  await expect(tabs(side)).toHaveText(["Active", "Archived"]);
  await expect(tab(side, "archived")).toHaveAttribute("aria-selected", "true");
  await expect(tab(side, "archived")).toBeFocused();
});

test("leaving the zero-count Shared tab by pointer puts focus on the tab pressed", async ({ page }) => {
  let ended = false;
  const { fake, list } = await open(page, (f) => {
    f.remote = [f.row(GPU, "gpu-box", GRANT_A, "Tune the render queue", true)];
    const live = f.onRead;
    f.onRead = (...a) => (ended ? GONE : live(...a));
  });
  await selectScope(list, "shared");
  await remote(list).first().locator("a").click();
  fake.remote = [];
  ended = true;
  await expect(page.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
  const side = await showList(page);
  await expect(tabs(side)).toHaveText(["Active", "Archived", "Shared"]);
  await tab(side, "active").click();
  await expect(tabs(side)).toHaveText(["Active", "Archived"]);
  await expect(tab(side, "active")).toBeFocused();
  await expect(local(side)).toHaveCount(2);
});
