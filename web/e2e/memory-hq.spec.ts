/** #2036: one Memory page, HQ capture/recall separation, source provenance, shared search. */
import { expect, test } from "@playwright/test";
import { MEMORY_PATH } from "../src/lib/routes";
import { CONFIG, commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

test("HQ is a notebook, capture is optional, and notes keep their source", async ({ page }) => {
  await page.clock.install();
  await commonMocks(page);
  await mockRoster(page);
  let capture = false;
  let arrived = false;
  let searches = 0;
  const mutations: string[] = [];
  const note = {
    slug: "research-sources", name: "Include primary sources", description: "Research findings",
    type: "decision", body: "Include primary source links with research findings.", size: 55,
    created: 100, updated: 100, revision: 1, links: [],
    session: { key: "", engine: "", kind: "", resolved: false },
    source: { kind: "explicit", source: "HQ conversation", quote: "Please include primary sources." },
  };
  const view = () => ({
    project: { id: "_hq", name: "HQ", color: "" }, specialist: null, notebooks: [], enabled: true,
    notes: arrived ? [note, { ...note, slug: "original-documents", name: "Check original documents" }] : [note], pending: [], refused: [], quarantine: [], withheld: { notes: 0, reason: "" },
    limits: { note_bytes: 4096, notes: 1000, inbox_files: 20 }, instructions: "",
    capture: { enabled: capture, revision: 0, generation: 0 },
  });
  await page.route("**/api/memory**", async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET") {
      expect(req.headers()["x-csrf-token"]).toBe(CONFIG.csrf);
      mutations.push(path);
    }
    if (path === "/api/memory") return route.fulfill({ json: { projects: [
      { id: "_hq", name: "HQ", color: "", enabled: true, notes: 1, pending: 0, quarantined: 0, last_write: 100 },
      { id: "p-1a2b3c4d", name: "Example", color: "", enabled: true, notes: 0, pending: 0, quarantined: 0, last_write: null },
    ] } });
    if (path === "/api/memory/_hq") return route.fulfill({ json: view() });
    if (path === "/api/memory/_hq/ingest") return route.fulfill({ json: { view: view() } });
    if (path === "/api/memory/_hq/capture") {
      capture = req.postDataJSON().enabled;
      return route.fulfill({ json: view().capture });
    }
    if (path === "/api/memory-search/status") return route.fulfill({ json: { state: "keyword-only", notes: 1, purge_pending: false } });
    if (path === "/api/memory-search") {
      searches++;
      expect(req.postDataJSON()).toEqual({ query: "citations", scopes: ["_hq"], limit: 10 });
      return route.fulfill({ json: { state: "ready", complete: true, hits: view().notes.map(n => ({ scope: "_hq", slug: n.slug, name: n.name, excerpt: n.body, href: "", source: n.source })) } });
    }
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  await page.goto(`${MEMORY_PATH}?project=_hq`);
  const control = page.getByRole("switch", { name: "Remember useful facts" });
  await expect(control).toBeVisible();
  await expect(control).toHaveAttribute("aria-checked", "false");
  await expect(page.getByText(/Explicit.*remember.*requests/)).toBeVisible();
  await expect(page.getByLabel("Agent access")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Import…", exact: true })).toHaveCount(0);
  await control.click();
  await expect(control).toHaveAttribute("aria-checked", "true");
  expect(mutations).toContain("/api/memory/_hq/capture");
  const list = page.getByRole("button", { name: "List", exact: true });
  if (await list.isVisible()) await list.click();
  // "citations" is absent from the note. Only the shared server's semantic result selects it.
  await page.getByRole("textbox", { name: "Search notes" }).fill("citations");
  await expect(page.getByText("Keyword and semantic search · up to 10 notes")).toBeVisible();
  // Another session's note must join this unchanged query after the normal visible refresh.
  const before = searches;
  arrived = true;
  await page.clock.fastForward(30_000);
  await expect.poll(async () => {
    await page.clock.fastForward(350);
    return searches;
  }).toBeGreaterThan(before);
  await expect(page.getByRole("button", { name: /Check original documents/ })).toBeVisible();
  const row = page.getByRole("button", { name: /Include primary sources/ });
  if (await row.count()) await row.first().click();
  else await page.getByTestId("memory-node-research-sources").click();
  await expect(page.getByRole("complementary", { name: "Note: Include primary sources" })).toContainText("Please include primary sources.");
  const box = await control.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
