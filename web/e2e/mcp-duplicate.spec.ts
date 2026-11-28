/** Library → MCP servers → ⋯ → Duplicate… (#2430): two logins for one service in one project, on a
 *  real browser at desktop and phone size (390 × 844), against `mcpFixture.ts`'s stand-in for the
 *  routes (`mcp_registry.duplicate`'s own rules are pinned in `tests/test_mcp_duplicate.py`). The
 *  pages, router and every request they make are real; what the requests SAID is asserted from the
 *  fake's call log.
 *
 *  Pinned: the dialog suggests the first free `<name>_N`, shows ONE row per distinct credential
 *  variable with every setting that reads it, and lands on the copy's Projects tab, where an
 *  unbound copy reads Missing with Use shared; a new credential is stored in Variables before the
 *  duplicate and exactly once, even across a server-side name clash, a lost response (one
 *  request_id, one copy) and a stale revision; a name already listed is refused on the name field
 *  before anything is sent; an empty library says so and starts on New credential; Cancel after a
 *  stored credential says it stays in Variables and deletes nothing. Every control is ≥ 44 px on the
 *  phone, the dialog scrolls to its actions, and nothing scrolls sideways. */
import { mkdirSync } from "node:fs";

import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

import { mcpServerPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";
import { NOW, addCheck, emptyFake, mockMcp, passedRow, record, type Fake } from "./mcpFixture";

test.setTimeout(60_000);

const SHOTS = "/tmp/2430-shots";
const VALUE = "personal-app-password-value";
const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

test.beforeEach(async ({ page }) => {
  if (isPhone(page)) await page.setViewportSize({ width: 390, height: 844 });
});

/** A PyPI Nextcloud server, installed and checked, whose app password two settings read from one
 *  variable (`nc_pass`, bound to Atlas). The library also holds `nc_pass_work`. */
function nextcloudFake(): { f: Fake; id: string } {
  const f = emptyFake();
  const source = { kind: "pypi" as const, package: "nextcloud-mcp-server", version: "0.21.0" };
  const rec = record({
    name: "nextcloud",
    label: "Nextcloud",
    source,
    transport: "stdio",
    settings: [
      { key: "NEXTCLOUD_HOST", kind: "plain", value: "https://cloud.example.org", deliver: "env" },
      { key: "NEXTCLOUD_APP_PASSWORD", kind: "credential", variable: "nc_pass", prefix: "", deliver: "env" },
      { key: "NEXTCLOUD_PASSWORD", kind: "credential", variable: "nc_pass", prefix: "", deliver: "env" },
    ],
    projects: ["p_atlas"],
    installed: { installed_at: NOW - 7200, integrity: "sha256:abc", source },
  });
  f.servers.push(rec);
  f.variables.push(
    { id: "v_nc", name: "nc_pass", kind: "secret", projects: ["p_atlas"], updated_at: NOW - 999 },
    { id: "v_nc_work", name: "nc_pass_work", kind: "secret", projects: [], updated_at: NOW - 999 },
  );
  addCheck(f, rec, passedRow(rec.id, "shared", 1, NOW - 3000));
  return { f, id: rec.id };
}

async function openDuplicate(page: Page, id: string) {
  await page.goto(mcpServerPath(id));
  await page.getByTestId("mcp-server-menu").click();
  await page.getByRole("menuitem", { name: "Duplicate…" }).click();
  const dialog = page.getByTestId("mcp-duplicate-dialog");
  await expect(dialog).toBeVisible();
  return dialog;
}

const posts = (f: Fake, path: RegExp) => f.calls.filter((c) => c.method === "POST" && path.test(c.path));
const dupPosts = (f: Fake) => posts(f, /\/duplicate$/);
const varPosts = (f: Fake) => posts(f, /^\/api\/variables$/);

async function shot(page: Page, info: TestInfo, name: string, focus?: Locator) {
  mkdirSync(SHOTS, { recursive: true });
  if (focus) await focus.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await page.screenshot({ path: `${SHOTS}/${name}-${info.project.name}.png` });
}

/** On the phone: every control the dialog offers is ≥ 44 px tall, the actions are reachable, and
 *  nothing (dialog or page) scrolls sideways. */
async function phoneChecks(page: Page, dialog: Locator) {
  if (!isPhone(page)) return;
  const controls = dialog.locator("input, select, button");
  for (let i = 0; i < (await controls.count()); i++) {
    const c = controls.nth(i);
    if (!(await c.isVisible())) continue;
    await c.scrollIntoViewIfNeeded();
    const box = (await c.boundingBox())!;
    expect(box.height, `${await c.evaluate((el) => el.outerHTML.slice(0, 80))}`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  const go = dialog.getByTestId("mcp-duplicate-go");
  await go.scrollIntoViewIfNeeded();
  await expect(go).toBeInViewport();
  const widths = await dialog.evaluate((el) => ({
    dialog: el.scrollWidth - el.clientWidth,
    page: document.documentElement.scrollWidth - window.innerWidth,
  }));
  expect(widths).toEqual({ dialog: 0, page: 0 });
}

test("copy with an existing secret: one row per variable, lands on the copy's Projects tab", async ({ page }, info) => {
  const { f, id } = nextcloudFake();
  f.servers.push(record({ name: "nextcloud_2" })); // taken: the suggestion skips it
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);

  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("nextcloud_3");
  // Two settings read nc_pass: one row, both keys listed beneath it.
  const rows = dialog.getByTestId("mcp-dup-credential");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("NEXTCLOUD_APP_PASSWORD");
  await expect(rows).toContainText("NEXTCLOUD_PASSWORD");
  await expect(rows).toContainText("nextcloud reads nc_pass");
  const pick = dialog.getByLabel("Variable for nc_pass");
  await expect(pick).toHaveValue("nc_pass"); // the original's, by default
  await expect(dialog).toContainText("Plain settings are copied as they are (NEXTCLOUD_HOST)");
  await expect(dialog).toContainText("The copy is enabled in no project and needs its own check.");

  await dialog.getByLabel("Name", { exact: true }).fill("nextcloud_work");
  await dialog.getByLabel("Label (optional)").fill("Nextcloud (work)");
  await pick.selectOption("nc_pass_work");
  await phoneChecks(page, dialog);
  await shot(page, info, "1-dialog-existing", dialog);
  await dialog.getByTestId("mcp-duplicate-go").click();

  const copy = f.servers.find((s) => s.name === "nextcloud_work")!;
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(copy.id)}\\?tab=projects$`));
  await expect(page.getByTestId("mcp-flash")).toContainText(
    "Duplicated nextcloud as nextcloud_work, reading nc_pass_work. It is enabled in no project",
  );
  expect(varPosts(f)).toHaveLength(0);
  const sent = dupPosts(f)[0].body as Record<string, unknown>;
  expect(sent).toMatchObject({ name: "nextcloud_work", label: "Nextcloud (work)", variables: { nc_pass: "nc_pass_work" } });
  expect(sent.request_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(Object.keys(sent).sort()).toEqual(["expected_updated_at", "label", "name", "request_id", "variables"]);

  // nc_pass_work is bound to no project: each row needs a binding (Use shared) or its own, then a check.
  const atlas = page.locator('[data-testid="mcp-project-row"][data-project="p_atlas"]');
  await expect(atlas).toContainText("Missing");
  await expect(atlas.getByRole("button", { name: "Use shared" })).toBeVisible();
  await expect(page.getByTestId("mcp-switch-p_atlas")).not.toBeChecked();
  await shot(page, info, "2-copy-projects");
  await atlas.getByRole("button", { name: "Use shared" }).click();
  await expect(atlas).toContainText("Shared");
  await expect(atlas.getByRole("button", { name: "Run check" })).toBeVisible();
  await expect(atlas).toContainText("Not run"); // the original's passing check never counts for the copy
});

test("copy with a new secret: stored once, even across a server-side name clash", async ({ page }, info) => {
  const { f, id } = nextcloudFake();
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue("nextcloud_2");
  await dialog.getByLabel("Name", { exact: true }).fill("nextcloud_personal");
  await dialog.getByLabel("Credential source for nc_pass").selectOption("new");
  await expect(dialog.getByLabel("Variable name for nc_pass")).toHaveValue("nc_pass_2");
  await dialog.getByLabel("Variable name for nc_pass").fill("nc_pass_personal");
  await dialog.getByLabel("New credential value for nc_pass").fill(VALUE);
  await phoneChecks(page, dialog);
  await shot(page, info, "3-dialog-new", dialog);

  // Someone else registers the name after the dialog read the list: only the route can refuse it.
  f.servers.push(record({ name: "nextcloud_personal" }));
  await dialog.getByTestId("mcp-duplicate-go").click();
  const nameField = dialog.getByLabel("Name", { exact: true });
  await expect(dialog.getByRole("alert").filter({ hasText: "A server named nextcloud_personal exists" })).toBeVisible();
  await expect(nameField).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByTestId("mcp-dup-stored")).toContainText("Stored in Variables: nc_pass_personal. It stays there");
  // The stored credential is now an existing pick: nothing to type again.
  await expect(dialog.getByLabel("Variable for nc_pass")).toHaveValue("nc_pass_personal");
  await shot(page, info, "4-dialog-clash", dialog);
  expect(varPosts(f)).toHaveLength(1);
  expect(varPosts(f)[0].body).toMatchObject({ name: "nc_pass_personal", kind: "secret", value: VALUE });

  await nameField.fill("nextcloud_home");
  await dialog.getByTestId("mcp-duplicate-go").click();
  const copy = () => f.servers.find((s) => s.name === "nextcloud_home");
  await expect.poll(() => copy()?.id).toBeTruthy();
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(copy()!.id)}\\?tab=projects$`));
  expect(varPosts(f)).toHaveLength(1); // never stored again
  const [first, second] = dupPosts(f).map((c) => c.body as Record<string, unknown>);
  expect(second).toMatchObject({ name: "nextcloud_home", variables: { nc_pass: "nc_pass_personal" } });
  expect(second.request_id).toBe(first.request_id);
  for (const c of dupPosts(f)) expect(JSON.stringify(c.body)).not.toContain(VALUE);
  expect(copy()!.settings.filter((s) => s.kind === "credential").map((s) => (s as { variable: string }).variable)).toEqual([
    "nc_pass_personal",
    "nc_pass_personal",
  ]);
});

test("a name already listed is refused on the name field; nothing is sent", async ({ page }) => {
  const { f, id } = nextcloudFake();
  f.servers.push(record({ name: "tracker" }));
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  const nameField = dialog.getByLabel("Name", { exact: true });
  await nameField.fill("tracker");
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect(dialog.getByRole("alert")).toHaveText("A server named tracker exists. Pick another name.");
  await expect(nameField).toHaveAttribute("aria-invalid", "true");
  await nameField.fill("Not A Name");
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect(dialog.getByRole("alert")).toContainText("Lowercase letters, digits, - and _");
  expect(dupPosts(f)).toHaveLength(0);
  expect(varPosts(f)).toHaveLength(0);
});

test("an empty library says so and starts on New credential", async ({ page }, info) => {
  const { f, id } = nextcloudFake();
  f.variables = [];
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  await expect(dialog.getByTestId("mcp-dup-empty-library")).toContainText("Variables has no secrets yet");
  await expect(dialog.getByLabel("Credential source for nc_pass")).toHaveValue("new");
  await expect(dialog.getByLabel("New credential value for nc_pass")).toBeVisible();
  await phoneChecks(page, dialog);
  await shot(page, info, "5-dialog-empty-library", dialog);
  await dialog.getByLabel("Credential source for nc_pass").selectOption("existing");
  await expect(dialog.getByLabel("Variable for nc_pass").locator("option", { hasText: "No other secrets in Variables" })).toHaveCount(1);
  // Duplicate without a value is refused in words, before anything is stored.
  await dialog.getByLabel("Credential source for nc_pass").selectOption("new");
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect(dialog).toContainText("At least 8 characters.");
  expect(varPosts(f)).toHaveLength(0);
});

test("Cancel after a stored credential says it stays in Variables and deletes nothing", async ({ page }, info) => {
  const { f, id } = nextcloudFake();
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  await dialog.getByLabel("Credential source for nc_pass").selectOption("new");
  await dialog.getByLabel("New credential value for nc_pass").fill(VALUE);
  f.servers.push(record({ name: "nextcloud_2" })); // the duplicate is refused after the store
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect(dialog.getByTestId("mcp-dup-stored")).toContainText("nc_pass_2");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("mcp-flash")).toHaveText("Not duplicated. nc_pass_2 stays in Variables.");
  await shot(page, info, "6-cancel-stays");
  expect(f.variables.map((v) => v.name)).toContain("nc_pass_2");
  expect(f.calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  expect(f.servers.filter((s) => s.name.startsWith("nextcloud_"))).toHaveLength(1); // only the one pushed above
});

test("a lost response: Duplicate again gets the copy already made, never a second one", async ({ page }) => {
  const { f, id } = nextcloudFake();
  f.dupLoseResponses = 1;
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect(dialog.getByTestId("mcp-dup-error")).toBeVisible();
  await dialog.getByTestId("mcp-duplicate-go").click();
  const copies = () => f.servers.filter((s) => s.name === "nextcloud_2");
  await expect.poll(() => copies().length).toBe(1);
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(copies()[0].id)}\\?tab=projects$`));
  const [a, b] = dupPosts(f).map((c) => (c.body as { request_id: string }).request_id);
  expect(b).toBe(a);
  expect(copies()).toHaveLength(1);
});

test("a stale revision: nothing is copied, the page is read again, Duplicate again works", async ({ page }) => {
  const { f, id } = nextcloudFake();
  await mockMcp(page, f);
  const dialog = await openDuplicate(page, id);
  f.servers[0].updated_at += 1; // changed elsewhere after the page read it
  const reread = page.waitForResponse((r) => r.request().method() === "GET" && r.url().endsWith(`/api/mcp/servers/${id}`));
  await dialog.getByTestId("mcp-duplicate-go").click();
  await reread;
  await expect(dialog.getByTestId("mcp-dup-error")).toContainText("This server changed since the dialog opened");
  expect(f.servers.filter((s) => s.name === "nextcloud_2")).toHaveLength(0);
  await dialog.getByTestId("mcp-duplicate-go").click();
  await expect.poll(() => f.servers.filter((s) => s.name === "nextcloud_2").length).toBe(1);
});
