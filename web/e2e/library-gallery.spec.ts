import { expect, test, type Locator, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { PLAYBOOKS_PATH, playbookPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

/** One Library gallery (#2144 P1): the whole card opens it (mouse, touch, keyboard), the card's own
 *  Export acts without navigating and downloads that item preselected, Import from file is a tile
 *  and a detail's Import…, and a detail import, once its results are closed, shows the new copy on
 *  the gallery without a page reload while the item you were on is unchanged. Desktop and phone. */

const REV = "a".repeat(64);
const playbook = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  source: "local",
  editable: true,
  ok: true,
  error: null,
  revision: REV,
  default: false,
  version: "1.0.0",
  domain: "development",
  summary: `${name}: implement and independently review a change.`,
  default_flow: "dev",
  flows: [
    {
      id: "dev",
      title: "Ship a change",
      steps: [
        { id: "implement", title: "Implement", actor: { kind: "operator" }, after: [], note: false },
      ],
    },
  ],
  ships: { materials: 1, runbooks: 0, templates: 1, variables: 0 },
  requires: { binaries: ["git"], connections: [] },
  connections: [],
  capabilities: [],
  ...extra,
});

const file = (path: string, text: string) => ({ path, size: text.length, text });
const previewItem = (id: string, title: string) => ({
  key: `playbook:${id}`,
  kind: "playbook",
  id,
  title,
  dest: id,
  status: "new",
  reason: null,
  depends_on: [],
  uses: [],
  files: [file("playbook.toml", "format = 5\n")],
});
const PREVIEW = {
  token: "a".repeat(32),
  expires_at: "2026-11-19T10:15:00Z",
  bundle: {
    digest: "9f3c" + "0".repeat(56) + "a71e",
    size: 4096,
    format: 1,
    created_at: "2026-11-18T14:02:00Z",
    app_version: "0.24.1",
    source: "Studio laptop",
    items: 2,
    files: 2,
  },
  items: [previewItem("release-notes", "Release notes"), previewItem("triage-kit", "Triage kit")],
};

interface World {
  writes: { method: string; path: string; body: unknown }[];
  listReads: number;
}

async function setup(page: Page, opts: { partial?: boolean } = {}): Promise<World> {
  await commonMocks(page);
  const world: World = { writes: [], listReads: 0 };
  let rows = [
    playbook("ship-it", "Ship it"),
    playbook("starter", "Starter", { source: "bundled", editable: false }),
  ];
  const imported: Record<string, { bundle: string; revision: string }> = {};
  await page.route("**/api/playbooks**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const id = path.split("/")[3];
    if (req.method() !== "GET")
      world.writes.push({ method: req.method(), path, body: req.postData() });
    if (!id) {
      world.listReads += 1;
      return route.fulfill({ json: { playbooks: rows, default: null, recovery_total: 0 } });
    }
    if (path.endsWith("/projects"))
      return route.fulfill({ json: { playbook_id: id, revision: REV, projects: [] } });
    const found = rows.find((r) => r.id === id);
    return found
      ? route.fulfill({
          json: {
            ...found,
            readme: "Keep the release branch untouched.",
            files: {},
            documents: { "playbook.toml": { format: 5, variables: [], materials: [] } },
            requires_present: { binaries: { git: true } },
            recovery_total: 0,
          },
        })
      : route.fulfill({ status: 404, json: { detail: "Not found" } });
  });
  await page.route("**/api/share/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET")
      world.writes.push({
        method: req.method(),
        path,
        body: path.endsWith("/preview") ? "file" : req.postDataJSON(),
      });
    if (path.endsWith("/catalog"))
      return route.fulfill({
        json: {
          imports: {},
          items: rows
            .filter((r) => r.source === "local")
            .map((r) => ({
              kind: "playbook",
              id: r.id,
              title: r.name,
              depends_on: [],
              exportable: true,
              reason: null,
              source: "local",
            })),
        },
      });
    if (path.endsWith("/imports")) return route.fulfill({ json: { imports: imported } });
    if (path.endsWith("/export"))
      return route.fulfill({ body: "bundle-bytes", headers: { "content-type": "application/x-tar" } });
    if (path.endsWith("/import/preview")) return route.fulfill({ json: PREVIEW });
    if (path.endsWith("/import")) {
      // The import creates NEW copies only; with `partial`, one of the two fails to write.
      const items = PREVIEW.items.map((i) => {
        const failed = opts.partial && i.id === "triage-kit";
        if (!failed) {
          rows = [...rows, playbook(i.id, i.title)];
          imported[i.key] = { bundle: PREVIEW.bundle.digest, revision: REV };
        }
        return {
          key: i.key,
          kind: i.kind,
          id: i.id,
          title: i.title,
          ...(failed
            ? { status: "failed", dest: i.dest, reason: "Nothing was written: a storage error (EIO)." }
            : { status: "created", dest: i.dest, reason: null }),
        };
      });
      return route.fulfill({
        json: { token: PREVIEW.token, bundle: PREVIEW.bundle.digest, repreview: null, items },
      });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return world;
}

const card = (page: Page, id: string) => page.getByTestId(`playbook-card-${id}`);

/** Marks the document, so a later read proves no full page load happened in between. */
async function markDocument(page: Page) {
  await page.evaluate(() => ((window as unknown as { __kept: boolean }).__kept = true));
}
async function documentKept(page: Page) {
  return page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept === true);
}

/** A point inside the card but away from its title link and its footer controls: the summary. */
async function bodyPoint(target: Locator) {
  const box = (await target.locator("p").first().boundingBox())!;
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

test("a tap anywhere on a card opens it — mouse, touch and keyboard", async ({ page }, info) => {
  await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  // One link per card, nothing interactive nested inside a link or a button (axe's
  // nested-interactive rule, asserted on the DOM).
  const nested = await page.getByTestId("playbook-grid").evaluate((grid) => ({
    nested: grid.querySelectorAll(
      "a a, a button, a input, a [tabindex], button a, button button, button input",
    ).length,
    links: [...grid.querySelectorAll("article")].map((a) => a.querySelectorAll("a").length),
  }));
  expect(nested.nested).toBe(0);
  expect(nested.links.every((n) => n === 1)).toBe(true);

  // Mouse (desktop) or touch (phone) on the summary, not the title.
  const target = card(page, "ship-it");
  const at = await bodyPoint(target);
  if (info.project.name === "mobile") await page.touchscreen.tap(at.x, at.y);
  else await page.mouse.click(at.x, at.y);
  await expect(page).toHaveURL(new RegExp(`${playbookPath("ship-it")}$`));
  await expect(page.getByRole("heading", { level: 1, name: "Ship it" })).toBeVisible();

  // Keyboard: the card's title link is its one tab stop; Enter opens it, and the card wears the
  // inset focus ring while it has focus.
  await page.goto(PLAYBOOKS_PATH);
  const link = card(page, "starter").getByRole("link", { name: "Starter" });
  await link.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(link).toBeFocused();
  await expect
    .poll(() => card(page, "starter").evaluate((el) => getComputedStyle(el).outlineStyle))
    .toBe("solid");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${playbookPath("starter")}$`));
});

test("a card's Export downloads that playbook without opening the card; bundled cards have none", async ({
  page,
}) => {
  const world = await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  await expect(card(page, "starter")).toBeVisible();
  await expect(card(page, "starter").getByTestId("card-export")).toHaveCount(0);
  await card(page, "ship-it").getByRole("button", { name: "Export Ship it" }).click();
  const dialog = page.getByTestId("share-export");
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}$`));
  await expect(dialog.getByLabel("Playbook Ship it")).toBeChecked();
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download bundle" }).click();
  expect((await download).suggestedFilename()).toMatch(/\.battlelab$/);
  expect(world.writes.filter((w) => w.path.endsWith("/export")).map((w) => w.body)).toEqual([
    { items: [{ kind: "playbook", id: "ship-it" }] },
  ]);
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}$`));
});

test("the Import from file tile and a detail's Import… open the import dialog; no header Import/Export", async ({
  page,
}) => {
  await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  const pg = page.getByTestId("playbooks-page");
  await expect(card(page, "ship-it")).toBeVisible();
  await expect(pg.getByRole("button", { name: "Import…" })).toHaveCount(0);
  await expect(pg.getByRole("button", { name: "Export…" })).toHaveCount(0);
  await page.getByTestId("library-import-tile").click();
  await expect(page.getByTestId("share-import")).toBeVisible();
  await page.getByTestId("share-import").getByRole("button", { name: /Close|Cancel/ }).first().click();
  await expect(page.getByTestId("share-import")).toHaveCount(0);

  await page.goto(playbookPath("ship-it"));
  const bar = page.getByRole("toolbar", { name: "Actions" });
  await expect(bar.getByRole("link", { name: "Edit playbook" })).toBeVisible();
  await expect(bar.getByRole("button", { name: "Export" })).toBeVisible();
  await bar.getByRole("button", { name: "Import…" }).click();
  await expect(page.getByTestId("share-import")).toBeVisible();
});

for (const partial of [false, true]) {
  test(`import from a detail view, close the results: the gallery shows the new ${partial ? "copy (partial success)" : "copies"} without a reload`, async ({
    page,
  }) => {
    const world = await setup(page, { partial });
    await page.goto(playbookPath("ship-it"));
    await expect(page.getByRole("heading", { level: 1, name: "Ship it" })).toBeVisible();
    await markDocument(page);
    await page.getByRole("toolbar", { name: "Actions" }).getByRole("button", { name: "Import…" }).click();
    const dialog = page.getByTestId("share-import");
    await dialog.getByTestId("share-file").setInputFiles({
      name: "kit.battlelab",
      mimeType: "application/x-tar",
      buffer: Buffer.from("bundle-bytes"),
    });
    await dialog.getByRole("button", { name: "Import 2 items" }).click();
    await expect(dialog.getByTestId("share-result-row-playbook:release-notes")).toHaveAttribute(
      "data-status",
      "created",
    );
    await expect(dialog.getByTestId("share-result-row-playbook:triage-kit")).toHaveAttribute(
      "data-status",
      partial ? "failed" : "created",
    );
    const reads = world.listReads;
    await dialog.getByRole("button", { name: "Done" }).click();
    await expect(dialog).toHaveCount(0);
    // Closing results that created something re-reads the list behind the detail.
    await expect.poll(() => world.listReads).toBeGreaterThan(reads);
    // The playbook you were on is unchanged: still open, and nothing was written to it.
    await expect(page.getByRole("heading", { level: 1, name: "Ship it" })).toBeVisible();
    expect(world.writes.filter((w) => w.path.startsWith("/api/playbooks"))).toEqual([]);

    await page.getByRole("link", { name: "All playbooks" }).click();
    await expect(card(page, "release-notes")).toBeVisible();
    await expect(card(page, "release-notes")).toContainText("Imported from a file");
    await expect(card(page, "triage-kit")).toHaveCount(partial ? 0 : 1);
    await expect(card(page, "ship-it")).toBeVisible();
    expect(await documentKept(page)).toBe(true);
  });
}

test("the gallery and the detail hold the 44px floor where a finger is the input", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "mobile", "the floor applies to a coarse pointer");
  await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  await expect(card(page, "ship-it")).toBeVisible();
  const controls = [
    page.getByRole("button", { name: "Export Ship it" }),
    page.getByRole("group", { name: "Show" }).getByRole("button").first(),
    page.getByLabel("Search playbooks"),
    page.getByTestId("library-import-tile"),
    page.getByTestId("library-new-tile"),
    page.getByRole("link", { name: "Yours", exact: true }),
  ];
  for (const c of controls)
    expect((await c.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );

  await page.goto(playbookPath("ship-it"));
  const bar = page.getByRole("toolbar", { name: "Actions" });
  for (const c of [
    bar.getByRole("link", { name: "Edit playbook" }),
    bar.getByRole("button", { name: "Export" }),
    bar.getByRole("button", { name: "Import…" }),
    bar.getByRole("button", { name: "More actions" }),
    page.getByRole("link", { name: "All playbooks" }),
  ])
    expect((await c.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The ⋯ opens the bottom sheet on a phone; its items act.
  await bar.getByRole("button", { name: "More actions" }).click();
  await expect(page.getByRole("menuitem", { name: "Duplicate" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Delete playbook…" })).toBeVisible();
});
