import { expect, test, type Page } from "@playwright/test";

// Real-browser checks for TEMPLATES — the instruction-template gallery + editor (#905 P2).
// Runs on desktop AND mobile: the one-column reflow, the bottom-sheet confirm and the 44px
// touch targets are layout facts a jsdom test cannot see. Network is fully mocked in the
// house pattern; the mock keeps an in-memory library so create / edit / delete round-trip,
// and `/api/uploads/*` serves a real 1×1 PNG so a thumbnail's `naturalWidth` proves the
// read-back URL was asked for (not just rendered as a string).

const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
  "base64",
);
const UP = "/home/u/.agent-sessions/uploads";
const LIMITS = {
  templates_max: 200,
  name_max: 120,
  description_max: 300,
  tags_max: 8,
  body_max: 100_000,
  fields_max: 12,
  label_max: 60,
  default_max: 500,
  images_max: 8,
  image_suffixes: [".gif", ".jpeg", ".jpg", ".png", ".webp"],
};

type Tpl = {
  id: string;
  name: string;
  description: string;
  tags: string[];
  body: string;
  fields: { name: string; label: string; default: string; required: boolean }[];
  images: { name: string; path: string }[];
  created_at: number;
  updated_at: number;
  used_count: number;
  last_used_at: number | null;
};

function tpl(over: Partial<Tpl> = {}): Tpl {
  return {
    id: "pr-review",
    name: "PR review checklist",
    description: "Review a PR against the checklist.",
    tags: ["review", "forgejo"],
    body: "Review PR {{pr_url}} against our checklist.",
    fields: [{ name: "pr_url", label: "PR link", default: "", required: true }],
    images: [{ name: "shot.png", path: `${UP}/20260903-1-shot.png` }],
    created_at: 1_788_400_000,
    updated_at: 1_788_430_000.5,
    used_count: 14,
    last_used_at: 1_788_432_000,
    ...over,
  };
}

type Req = { method: string; url: string; body: unknown };

async function mockShell(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/notifications**", (r) => r.fulfill({ json: { notifications: [] } }));
}

async function mockTemplates(page: Page, templates: Tpl[]) {
  const requests: Req[] = [];
  await page.route(/\/api\/templates(\?.*)?$/, async (r) => {
    const req = r.request();
    if (req.method() === "GET") return r.fulfill({ json: { templates, limits: LIMITS } });
    if (req.method() === "POST") {
      const body = req.postDataJSON() as Omit<Tpl, "id">;
      requests.push({ method: "POST", url: req.url(), body });
      const rec: Tpl = {
        ...body,
        id: body.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""),
        created_at: 1_788_500_000,
        updated_at: 1_788_500_000,
        used_count: 0,
        last_used_at: null,
      };
      templates.push(rec);
      return r.fulfill({ status: 201, json: rec });
    }
    return r.continue();
  });
  await page.route(/\/api\/templates\/[^/?]+(\?.*)?$/, async (r) => {
    const req = r.request();
    const url = new URL(req.url());
    const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
    const body = req.method() === "PATCH" ? (req.postDataJSON() as Record<string, unknown>) : null;
    requests.push({ method: req.method(), url: req.url(), body });
    const i = templates.findIndex((t) => t.id === id);
    if (i < 0) return r.fulfill({ status: 404, json: { detail: "unknown template" } });
    if (req.method() === "PATCH" && body) {
      const cur = templates[i];
      if (body.expected_updated_at !== cur.updated_at) {
        return r.fulfill({ status: 409, json: { detail: "changed", current: cur } });
      }
      const fields: Record<string, unknown> = { ...body };
      delete fields.expected_updated_at;
      const rec = { ...cur, ...(fields as Partial<Tpl>), updated_at: cur.updated_at + 1 };
      templates[i] = rec;
      return r.fulfill({ json: rec });
    }
    if (req.method() === "DELETE") {
      templates.splice(i, 1);
      return r.fulfill({ status: 204, body: "" });
    }
    return r.continue();
  });
  await page.route("**/api/upload", (r) =>
    r.fulfill({
      json: { path: `${UP}/20260903-2-ref.png`, name: "ref.png", stored: "20260903-2-ref.png" },
    }),
  );
  await page.route("**/api/uploads/*", (r) =>
    r.fulfill({ status: 200, contentType: "image/png", body: PNG }),
  );
  return requests;
}

async function openGalleryFromTheShell(page: Page, isMobile: boolean) {
  await page.goto("/");
  if (isMobile) await page.getByRole("button", { name: /open session list/i }).click();
  await page.getByRole("link", { name: "Templates", exact: true }).click();
  await expect(page).toHaveURL(/\/templates$/);
}

test.beforeEach(async ({ page }) => {
  await mockShell(page);
});

test("the topbar gear opens the gallery; cards carry a thumbnail read back by the stored name, tags and a meta line", async ({
  page,
  isMobile,
}) => {
  await mockTemplates(page, [
    tpl(),
    tpl({ id: "deploy", name: "Deploy watch", tags: ["ops"], images: [], fields: [], used_count: 0, last_used_at: null }),
  ]);
  await openGalleryFromTheShell(page, !!isMobile);

  const list = page.getByRole("list", { name: "Templates" });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  const first = list.getByRole("listitem").filter({ hasText: "PR review checklist" });
  const img = first.locator("img");
  // The bytes arrive through the fetch seam and render from an object URL — never a native
  // `/api/uploads/...` src (the Home Free tunnel, #907 review). naturalWidth proves they loaded.
  await expect(img).toHaveAttribute("src", /^blob:/);
  await expect(img).toHaveAttribute("data-upload-path", `${UP}/20260903-1-shot.png`);
  await expect.poll(() => img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBe(1);
  await expect(first).toContainText("1 field · 1 image · used 14×");
  await expect(first.getByText("review", { exact: true })).toBeVisible();

  // Tag chip narrows; the chips keep their unfiltered counts.
  await page.getByRole("button", { name: /^ops 1$/ }).click();
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await expect(page.getByRole("button", { name: /^review 1$/ })).toBeVisible();
  await page.getByRole("button", { name: /^All 2$/ }).click();
  await page.getByRole("searchbox", { name: /search templates/i }).fill("deploy");
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await expect(list).toContainText("Deploy watch");
});

test("create: name + instructions + an image → the preview shows the path, and the new card shows the thumbnail", async ({
  page,
}) => {
  const requests = await mockTemplates(page, []);
  await page.goto("/templates");
  await expect(page.getByText(/no templates yet/i)).toBeVisible();
  await page.getByRole("link", { name: /new template/i }).first().click();
  await expect(page).toHaveURL(/\/templates\/new$/);

  const save = page.getByRole("button", { name: /^save$/i });
  await expect(save).toBeDisabled();
  await page.getByLabel(/^name/i).fill("Match this mockup");
  await page.getByLabel(/instructions/i).fill("Implement the attached design for {{screen}} exactly.");
  await page.getByLabel(/choose images/i).setInputFiles({
    name: "ref.png",
    mimeType: "image/png",
    buffer: PNG,
  });
  const preview = page.getByLabel(/what the agent receives/i);
  await expect(preview).toContainText(`${UP}/20260903-2-ref.png`);
  await expect(page.getByRole("img", { name: "ref.png" })).toHaveAttribute("src", /^blob:/);
  await expect(page.getByText(/\{\{screen\}\} names no field/i)).toBeVisible();
  await expect(save).toBeEnabled();
  await save.click();

  await expect(page).toHaveURL(/\/templates$/);
  const post = requests.find((r) => r.method === "POST");
  expect(post?.body).toMatchObject({
    name: "Match this mockup",
    body: "Implement the attached design for {{screen}} exactly.",
    images: [{ name: "ref.png", path: `${UP}/20260903-2-ref.png` }],
  });
  const card = page.getByRole("listitem").filter({ hasText: "Match this mockup" });
  await expect(card.locator("img")).toHaveAttribute("data-upload-path", `${UP}/20260903-2-ref.png`);
  await expect(card.locator("img")).toHaveAttribute("src", /^blob:/);
  await expect(page.getByText(/saved “match this mockup”/i)).toBeVisible();
});

test("edit: leaving with unsaved edits asks first — via Cancel, a topbar gear and browser Back; save sends the loaded updated_at as the fence", async ({
  page,
  isMobile,
}) => {
  const requests = await mockTemplates(page, [tpl()]);
  await page.goto("/templates");
  await page.getByRole("link", { name: /edit pr review checklist/i }).click();
  const name = page.getByLabel(/^name/i);
  await expect(name).toHaveValue("PR review checklist");
  await name.fill("PR review checklist v2");
  await expect(page.getByText(/unsaved/i)).toBeVisible();

  // Cancel.
  await page.getByRole("button", { name: /^cancel$/i }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/unsaved changes/i);
  await dialog.getByRole("button", { name: /keep editing/i }).click();
  await expect(dialog).toBeHidden();
  await expect(name).toHaveValue("PR review checklist v2");

  // A shell navigation: the Settings gear (in the drawer on mobile). The router blocker holds
  // it — the first cut only wired this page's Cancel (#907 review).
  if (isMobile) await page.getByRole("button", { name: /open session list/i }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(/unsaved changes/i);
  await page.getByRole("dialog").getByRole("button", { name: /keep editing/i }).click();
  await expect(page).toHaveURL(/\/templates\/pr-review$/);
  if (isMobile) await page.keyboard.press("Escape");

  // Browser Back.
  await page.goBack();
  await expect(page.getByRole("dialog")).toContainText(/unsaved changes/i);
  await page.getByRole("dialog").getByRole("button", { name: /keep editing/i }).click();
  await expect(page).toHaveURL(/\/templates\/pr-review$/);
  await expect(name).toHaveValue("PR review checklist v2");

  await page.getByRole("button", { name: /^save$/i }).click();
  await expect(page).toHaveURL(/\/templates$/);
  const patch = requests.find((r) => r.method === "PATCH");
  expect(patch?.url).toMatch(/\/api\/templates\/pr-review$/);
  expect((patch?.body as Record<string, unknown>).expected_updated_at).toBe(1_788_430_000.5);
  await expect(page.getByRole("listitem")).toContainText("PR review checklist v2");
});

test("delete sits behind a confirm and carries the fence; the library empties into the empty state", async ({
  page,
}) => {
  const requests = await mockTemplates(page, [tpl()]);
  await page.goto("/templates");
  await page.getByRole("button", { name: /delete pr review checklist/i }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/image stays in the uploads folder/i);
  expect(requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
  await dialog.getByRole("button", { name: /^delete$/i }).click();
  await expect(page.getByText(/no templates yet/i)).toBeVisible();
  const del = requests.find((r) => r.method === "DELETE");
  expect(del?.url).toMatch(/\/api\/templates\/pr-review\?expected_updated_at=1788430000\.5$/);
});

test("mobile: one column, and every action is a real touch target", async ({ page, isMobile }) => {
  test.skip(!isMobile, "touch-target rule is a mobile layout fact");
  await mockTemplates(page, [tpl(), tpl({ id: "deploy", name: "Deploy watch", tags: ["ops"] })]);
  await page.goto("/templates");
  const list = page.getByRole("list", { name: "Templates" });
  const cards = list.getByRole("listitem");
  await expect(cards).toHaveCount(2);
  // One column: the second card starts below the first, at the same x.
  const a = (await cards.nth(0).boundingBox())!;
  const b = (await cards.nth(1).boundingBox())!;
  expect(b.y).toBeGreaterThan(a.y + a.height - 1);
  expect(Math.abs(b.x - a.x)).toBeLessThanOrEqual(1);

  for (const name of [/edit pr review checklist/i, /duplicate pr review checklist/i, /delete pr review checklist/i]) {
    const box = (await page.getByRole("link", { name }).or(page.getByRole("button", { name })).first().boundingBox())!;
    expect(box.height, String(name)).toBeGreaterThanOrEqual(44);
  }
  for (const name of [/^All 2$/, /^review 1$/, /^ops 1$/]) {
    expect((await page.getByRole("button", { name }).boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  expect((await page.getByRole("link", { name: /new template/i }).first().boundingBox())!.height).toBeGreaterThanOrEqual(44);
  // And the generic sweep over every control in the gallery — including the card-title link,
  // a second edit link that the enumeration above never covered (Hermes on #907, addendum).
  await expectTouchTargets(page, page.locator("main"));

  // The delete confirm is a bottom sheet whose every control — including the close — is 44×44.
  await page.getByRole("button", { name: /delete pr review checklist/i }).click();
  const dialog = page.getByRole("dialog");
  const vp = page.viewportSize()!;
  const dbox = (await dialog.boundingBox())!;
  expect(dbox.y + dbox.height).toBeGreaterThanOrEqual(vp.height - 1);
  await expectTouchTargets(page, dialog);
  await dialog.getByRole("button", { name: /^cancel$/i }).click();

  // The editor stacks: the preview sits below the form and is collapsible — and every control
  // in it (tag remove, field remove, image remove, the toggle, the footer) is 44×44.
  await page.goto("/templates/pr-review");
  const form = page.getByLabel(/^name/i);
  const toggle = page.getByRole("button", { name: /hide|show/i });
  await expect(toggle).toBeVisible();
  expect((await toggle.boundingBox())!.y).toBeGreaterThan((await form.boundingBox())!.y);
  await expect(page.getByRole("img", { name: "shot.png" })).toBeVisible();
  await expectTouchTargets(page, page.locator("main"));
});

/** Every visible interactive control inside `scope` (buttons, links, checkboxes — text inputs
 *  excluded) measures at least 44×44 CSS px. Names each offender. */
async function expectTouchTargets(page: Page, scope: ReturnType<Page["locator"]>) {
  // Text inputs and textareas are targets too (docs/design.md §8) — the first sweep left them
  // out and shipped 26-34px fields (Hermes on #907, round 2).
  const controls = scope.locator(
    'button:visible, a[href]:visible, input:visible, textarea:visible, [role="button"]:visible',
  );
  const n = await controls.count();
  expect(n).toBeGreaterThan(0);
  const small: string[] = [];
  for (let i = 0; i < n; i++) {
    const el = controls.nth(i);
    // An input's hit area is its wrapping <label>, when it has one (the checkbox, the search).
    const target = (await el.evaluate((e) => e.tagName === "INPUT" && !!e.closest("label")))
      ? el.locator("xpath=ancestor::label[1]")
      : el;
    const box = await target.boundingBox();
    if (!box) continue;
    if (box.width < 44 || box.height < 44) {
      const label =
        (await el.getAttribute("aria-label")) || (await el.innerText()).trim() || (await el.getAttribute("class"));
      small.push(`${label}: ${Math.round(box.width)}×${Math.round(box.height)}`);
    }
  }
  expect(small, `controls under 44×44: ${small.join(", ")}`).toEqual([]);
}


test("the gallery loads only the images near the viewport, not one full-size download per card", async ({
  page,
}) => {
  const many = Array.from({ length: 40 }, (_, i) =>
    tpl({
      id: `t${i}`,
      name: `Template ${i}`,
      images: [{ name: `img${i}.png`, path: `${UP}/20260903-${100 + i}-img${i}.png` }],
    }),
  );
  await mockTemplates(page, many);
  let fetched = 0;
  await page.route("**/api/uploads/*", (r) => {
    fetched += 1;
    return r.fulfill({ status: 200, contentType: "image/png", body: PNG });
  });
  await page.goto("/templates");
  await expect(page.getByRole("list", { name: "Templates" }).getByRole("listitem")).toHaveCount(40);
  await page.waitForTimeout(800);
  const initial = fetched;
  expect(initial).toBeGreaterThan(0);
  expect(initial, "only the cards near the viewport should have fetched").toBeLessThan(40);
  // Scrolling further brings more in — progressively, never all at once.
  await page.getByRole("list", { name: "Templates" }).getByRole("listitem").last().scrollIntoViewIfNeeded();
  await expect.poll(() => fetched, { timeout: 5000 }).toBeGreaterThan(initial);
  expect(fetched).toBeLessThan(40);
});

test("mobile: the four card actions never clip — at 320px and at the 800px breakpoint edge (#908 round 7)", async ({ page, isMobile }) => {
  test.skip(!isMobile, "narrow-width layout fact");
  await mockTemplates(page, [tpl()]);
  for (const width of [320, 800]) {
    await page.setViewportSize({ width, height: 700 });
    await page.goto("/templates");
    await expect(page.getByRole("list", { name: "Templates" }).getByRole("listitem").first()).toBeVisible();
    for (const name of [
      /^use pr review checklist$/i,
      /^edit pr review checklist$/i,
      /^duplicate pr review checklist$/i,
      /^delete pr review checklist$/i,
    ]) {
      const box = (await page.getByRole("link", { name }).or(page.getByRole("button", { name })).first().boundingBox())!;
      expect(box.x, `${String(name)} @${width}`).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, `${String(name)} @${width}`).toBeLessThanOrEqual(width + 0.5);
      expect(box.height, `${String(name)} @${width}`).toBeGreaterThanOrEqual(44);
    }
    // No horizontal overflow hidden behind a suppressed page scroll.
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }
});
