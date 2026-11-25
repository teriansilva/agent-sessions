import { expect, test, type Locator, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { TEMPLATES_PATH, VARIABLES_PATH } from "../src/lib/routes";

/** Share bundles carry prompt templates and variable definitions (#1714 P3, boards A–E in comment
 *  114659): a template brings the variables its fields use, a secret travels as a definition only
 *  and arrives empty, a renamed variable is followed by its template, and the result links to the
 *  place the empty secret is filled in — desktop and a 320 px phone, by pointer and by keyboard.
 *  The backend is mocked; every request the SPA sends is asserted. */

const file = (path: string, text: string) => ({ path, size: text.length, text });

const CATALOG = {
  imports: {},
  items: [
    { kind: "variable", id: "api_url", title: "API URL", depends_on: [], exportable: true, reason: null, source: "local", note: null, secret: false },
    {
      kind: "variable",
      id: "deploy_token",
      title: "Deploy token",
      depends_on: [],
      exportable: true,
      reason: null,
      source: "local",
      note: "Definition only: the export never adds its stored value.",
      secret: true,
    },
    { kind: "variable", id: "region", title: "Region", depends_on: [], exportable: true, reason: null, source: "local", note: null, secret: false },
    {
      kind: "template",
      id: "deploy-note",
      title: "Deploy note",
      depends_on: ["variable:api_url", "variable:deploy_token"],
      exportable: true,
      reason: null,
      source: "local",
      note: null,
    },
    {
      kind: "template",
      id: "leaky-prompt",
      title: "Leaky prompt",
      depends_on: [],
      exportable: false,
      reason: "Its body holds one of your stored secrets; remove it to export.",
      source: "local",
      note: null,
    },
  ],
};

/** Extra variables make the preview a long list: confirm must stay reachable by scrolling. */
const FILLER = Array.from({ length: 14 }, (_, n) => ({
  key: `variable:extra_${n}`,
  kind: "variable",
  id: `extra_${n}`,
  title: `Extra ${n}`,
  dest: `extra_${n}`,
  status: "new",
  reason: null,
  depends_on: [],
  uses: [],
  files: [file("variable.json", '{"format": 1, "kind": "text"}\n')],
  variable: { kind: "text", value_type: "text", arrives_empty: false },
}));

const PREVIEW = {
  token: "c".repeat(32),
  expires_at: "2026-10-30T10:27:00Z",
  bundle: {
    digest: "7a3c" + "0".repeat(56) + "e10f",
    size: 41984,
    format: 1,
    created_at: "2026-10-30T09:12:00Z",
    app_version: "0.27.0",
    source: "Studio laptop",
    items: 3 + FILLER.length,
    files: 4 + FILLER.length,
  },
  items: [
    {
      key: "variable:api_url",
      kind: "variable",
      id: "api_url",
      title: "API URL",
      dest: "api_url_2",
      status: "renamed",
      reason: null,
      depends_on: [],
      uses: [],
      files: [file("variable.json", '{"format": 1, "kind": "text", "value": "https://api.example.test"}\n')],
      variable: { kind: "text", value_type: "text", arrives_empty: false },
    },
    {
      key: "variable:deploy_token",
      kind: "variable",
      id: "deploy_token",
      title: "Deploy token",
      dest: "deploy_token",
      status: "new",
      reason: null,
      depends_on: [],
      uses: [],
      files: [file("variable.json", '{"format": 1, "kind": "secret", "value": null}\n')],
      variable: { kind: "secret", value_type: "text", arrives_empty: true },
    },
    {
      key: "template:deploy-note",
      kind: "template",
      id: "deploy-note",
      title: "Deploy note",
      dest: "deploy-note-2",
      status: "renamed",
      reason: null,
      depends_on: ["variable:api_url", "variable:deploy_token"],
      uses: [{ from: "api_url", to: "api_url_2" }],
      files: [file("template.json", '{"format": 1, "body": "Deploy to {{api_url}}"}\n'), { path: "images/1.png", size: 18432, binary: true }],
      template: { fields: 3, images: 1, variables: ["api_url_2", "deploy_token"] },
    },
    ...FILLER,
  ],
};

const VARIABLES = {
  variables: [
    { id: "id-1", name: "api_url", kind: "text", definition: { value_type: "text" }, set: true, value: "https://mine.test", created_at: 1, updated_at: 1 },
    { id: "id-2", name: "api_url_2", kind: "text", definition: { value_type: "text" }, set: true, value: "https://api.example.test", created_at: 2, updated_at: 2 },
    { id: "id-3", name: "deploy_token", kind: "secret", definition: { value_type: "text" }, set: false, needs_reentry: false, created_at: 3, updated_at: 3 },
  ],
  collections: [{ id: "c1", name: "Prod", variable_ids: ["id-1"], created_at: 1, updated_at: 1 }],
  migration_required: false,
  limits: { variables_max: 100, collections_max: 50, secret_min: 8, value_max: 2000, name_max: 32 },
};

type Writes = { path: string; body: unknown }[];

async function setup(page: Page) {
  await commonMocks(page);
  const writes: Writes = [];
  await page.route("**/api/templates", (r) =>
    r.fulfill({
      json: {
        templates: [],
        limits: { templates_max: 200, name_max: 120, description_max: 300, tags_max: 8, body_max: 100000, fields_max: 12, label_max: 60, default_max: 500, images_max: 8, image_suffixes: [".png"] },
      },
    }),
  );
  await page.route("**/api/templates/suggestions", (r) => r.fulfill({ json: { result: null, configured: false } }));
  await page.route("**/api/variables", (r) => r.fulfill({ json: VARIABLES }));
  await page.route("**/api/variables/secret-channels**", (r) => r.fulfill({ json: { channels: [] } }));
  await page.route("**/api/share/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET")
      writes.push({ path, body: path.endsWith("/preview") ? (request.postDataBuffer()?.length ?? 0) : request.postDataJSON() });
    if (path.endsWith("/catalog")) return route.fulfill({ json: CATALOG });
    if (path.endsWith("/imports")) return route.fulfill({ json: { imports: {} } });
    if (path.endsWith("/export")) return route.fulfill({ body: "bundle-bytes", headers: { "content-type": "application/x-tar" } });
    if (path.endsWith("/import/preview")) return route.fulfill({ json: PREVIEW });
    if (path.endsWith("/import")) {
      const body = request.postDataJSON() as { selection: string[] };
      return route.fulfill({
        json: {
          token: PREVIEW.token,
          bundle: PREVIEW.bundle.digest,
          repreview: null,
          items: PREVIEW.items.map((i) => ({
            key: i.key,
            kind: i.kind,
            id: i.id,
            title: i.title,
            status: body.selection.includes(i.key) ? "created" : "not_selected",
            dest: body.selection.includes(i.key) ? i.dest : null,
            reason: null,
            retry: false,
          })),
        },
      });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return writes;
}

test.beforeEach(async ({ page }, testInfo) => {
  if (testInfo.project.name === "mobile") await page.setViewportSize({ width: 320, height: 720 });
});

async function fits(page: Page, dialog: Locator) {
  const box = await dialog.boundingBox();
  const width = page.viewportSize()!.width;
  expect(box!.x).toBeGreaterThanOrEqual(-1);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  const overflow = await dialog.evaluate((el) => {
    const body = el.querySelector("[class*='body']") as HTMLElement | null;
    return body ? body.scrollWidth - body.clientWidth : 0;
  });
  expect(overflow).toBeLessThanOrEqual(1);
}

async function tall44(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(43.5);
}

async function openPreview(page: Page, path: string | null = TEMPLATES_PATH) {
  if (path) await page.goto(path);
  await page.getByRole("button", { name: "Import…" }).click();
  const dialog = page.getByTestId("share-import");
  await dialog.getByTestId("share-file").setInputFiles({
    name: "deploy-kit.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("bundle-bytes"),
  });
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  return dialog;
}

test("export: a template brings its variables, locked; a secret is a definition only; a leaky one can't be chosen", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto(TEMPLATES_PATH);
  await page.getByRole("button", { name: "Export…" }).click();
  const dialog = page.getByTestId("share-export");
  await dialog.getByLabel("Template Deploy note").check();
  await expect(dialog.getByLabel("Variable API URL (included)")).toBeDisabled();
  await expect(dialog.getByLabel("Secret Deploy token (included)")).toBeDisabled();
  const secret = dialog.getByTestId("share-export-row-variable:deploy_token");
  await expect(secret).toContainText("Included — needed by deploy-note");
  await expect(secret).toContainText("Definition only: the export never adds its stored value.");
  await expect(dialog.getByLabel("Template Leaky prompt")).toBeDisabled();
  await expect(dialog.getByTestId("share-export-row-template:leaky-prompt")).toContainText("stored secrets");
  await expect(dialog.getByText("What templates and variables carry.")).toBeVisible();
  await expect(page.getByTestId("share-export-summary")).toContainText("3 items · 1 chosen · 2 included");
  await fits(page, dialog);
  const download = page.waitForEvent("download");
  const button = dialog.getByRole("button", { name: "Download bundle" });
  await tall44(button);
  await button.click();
  await download;
  expect(writes).toEqual([{ path: "/api/share/export", body: { items: [{ kind: "template", id: "deploy-note" }] } }]);
});

test("import: a secret arrives empty, a renamed variable is followed, a long list still reaches Import by keyboard", async ({
  page,
}) => {
  const writes = await setup(page);
  const dialog = await openPreview(page);
  await fits(page, dialog);
  const secret = dialog.getByTestId("share-preview-row-variable:deploy_token");
  await expect(secret.getByTestId("share-arrives-empty")).toBeVisible();
  await expect(secret).toContainText("It arrives empty: set it on the Variables page");
  await expect(dialog.getByTestId("share-preview-row-variable:api_url")).toContainText(
    "templates in this file that use it are pointed at api_url_2",
  );
  const tpl = dialog.getByTestId("share-preview-row-template:deploy-note");
  await expect(tpl).toContainText("3 fields · 1 image · uses api_url_2, deploy_token");
  await expect(page.getByTestId("share-preview-summary")).toContainText("1 arrives empty");
  // Deselecting the secret refuses the template at once; ticking it again brings it back.
  await secret.getByLabel("Import Secret Deploy token").uncheck();
  await expect(tpl).toHaveAttribute("data-state", "refused");
  await expect(tpl).toContainText("Needs variable deploy_token, which isn't being imported");
  await secret.getByLabel("Import Secret Deploy token").check();
  await expect(tpl).toHaveAttribute("data-state", "import");
  // The image can't be shown, so Import waits for the acknowledgement — given by keyboard.
  const importButton = dialog.getByRole("button", { name: `Import ${PREVIEW.items.length} items` });
  await expect(importButton).toBeDisabled();
  const ack = dialog.getByTestId("share-blind-ack").getByRole("checkbox");
  await ack.focus();
  await page.keyboard.press("Space");
  await expect(ack).toBeChecked();
  await tall44(dialog.getByTestId("share-blind-ack"));
  await importButton.scrollIntoViewIfNeeded();
  await expect(importButton).toBeInViewport();
  await tall44(importButton);
  await importButton.focus();
  await page.keyboard.press("Enter");
  const created = dialog.getByTestId("share-result-row-variable:deploy_token");
  await expect(created).toHaveAttribute("data-status", "created");
  await expect(created).toContainText("It arrived empty.");
  await expect(dialog.getByTestId("share-result-row-template:deploy-note").getByRole("link", { name: /Open/ })).toHaveAttribute(
    "href",
    `${TEMPLATES_PATH}/deploy-note-2`,
  );
  const confirm = writes.find((w) => w.path === "/api/share/import");
  expect([...(confirm?.body as { selection: string[] }).selection].sort()).toEqual(PREVIEW.items.map((i) => i.key).sort());
  expect(confirm?.body).not.toHaveProperty("settle");
  await fits(page, dialog);
  // Set value lands on Library → Variables, filtered to the empty secret.
  const setValue = created.getByRole("link", { name: /Set value/ });
  await tall44(setValue);
  await setValue.click();
  await expect(page).toHaveURL(new RegExp(`${VARIABLES_PATH}\\?q=deploy_token$`));
  await expect(page.getByRole("article", { name: "deploy_token" })).toBeVisible();
  await expect(page.getByRole("article", { name: "api_url" })).toHaveCount(0);
});

test("import from Library → Variables: the same pair is there, the result fits the phone, Set value finds the secret", async ({
  page,
}) => {
  await setup(page);
  await page.goto(VARIABLES_PATH);
  // A collection is selected while importing: Set value must still land on the new secret.
  await page.getByRole("button", { name: /Prod/ }).click();
  await expect(page.getByRole("article", { name: "deploy_token" })).toHaveCount(0);
  const dialog = await openPreview(page, null);
  await dialog.getByTestId("share-blind-ack").getByRole("checkbox").check();
  await dialog.getByRole("button", { name: `Import ${PREVIEW.items.length} items` }).click();
  await expect(dialog.getByTestId("share-result-row-variable:api_url")).toContainText("api_url_2 (renamed from api_url)");
  await fits(page, dialog);
  const done = dialog.getByRole("button", { name: "Done" });
  await tall44(done);
  await dialog.getByTestId("share-result-row-variable:deploy_token").getByRole("link", { name: /Set value/ }).click();
  await expect(page.getByRole("article", { name: "deploy_token" })).toBeVisible();
});
