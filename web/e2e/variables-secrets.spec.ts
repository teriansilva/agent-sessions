import { expect, test, type Page } from "@playwright/test";
import { TOUCH_FLOOR } from "./touchTarget";

// Real-browser checks for secrets in Library → Variables (#1786 P3): a secret file row (file name
// kept as typed, size, the AGENTS chip), the secret editor's type choice, file drop zone, agent
// access switch, project picker and access log — every control a real touch target and nothing
// overflowing a phone — and the save order a new secret file takes: create, upload (multipart),
// agent access, projects. The API is an in-page stub; the stored file's bytes never come back.
// Runs on desktop AND mobile, light AND dark.

const LIMITS = { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8, collections_max: 50 };
const KEY_BYTES = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQgE2ESECRET\n";

type Row = Record<string, unknown>;

async function boot(page: Page, theme: "light" | "dark") {
  const calls: string[] = [];
  let clock = 100;
  const rows: Row[] = [
    { id: "id-key", name: "asc_api_key", kind: "secret_file", definition: { value_type: "text", label: "asc_api_key", help: "App Store Connect API key for fastlane" }, set: true, needs_reentry: false, agent_access: true, projects: ["atlas"], file: { name: "AuthKey_7F3K2L9QXA.p8", size: 257 }, created_at: 1, updated_at: 10 },
    { id: "id-tok", name: "forgejo_token", kind: "secret", definition: { value_type: "text", label: "forgejo_token" }, set: true, needs_reentry: false, agent_access: false, projects: [], created_at: 1, updated_at: 10 },
    { id: "id-host", name: "default_branch", kind: "text", definition: { value_type: "text", label: "default_branch" }, set: true, value: "main", created_at: 1, updated_at: 10 },
  ];
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/config", (r) => r.fulfill({ json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [
    { id: "claude", kind: "agent", runtime: "pty", label: "Claude Code", status: "active", display: { name: "claude", badge: "cc", order: 1, color: "#e8a33c" }, tools: { mcp: "claude" } },
    { id: "gemini", kind: "agent", runtime: "pty", label: "Gemini CLI", status: "active", display: { name: "gemini", badge: "gm", order: 2, color: "#4aa3df" }, tools: {} },
  ] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [
    { id: "atlas", name: "Atlas iOS", color: "#e8a33c", folders: [], default_folder: "", archived: false },
    { id: "beta", name: "Beta", color: "#4aa3df", folders: [], default_folder: "", archived: false },
  ] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/notifications**", (r) => r.fulfill({ json: { notifications: [] } }));
  await page.route(/\/api\/variables(\/.*)?$/, async (r) => {
    const req = r.request();
    const url = new URL(req.url());
    const path = url.pathname;
    calls.push(`${req.method()} ${path.replace(/^\/api\/variables/, "") || "/"}`);
    if (req.method() === "GET" && path === "/api/variables") {
      return r.fulfill({ json: { variables: rows, collections: [], migration_required: false, limits: LIMITS } });
    }
    if (req.method() === "GET" && path.endsWith("/access-log")) {
      return r.fulfill({ json: { entries: [
        { at: Date.now() / 1000, project_id: "atlas", engine: "claude", session_key: "claude:release-24", outcome: "materialized", reason: null },
        { at: Date.now() / 1000 - 3600, project_id: "beta", engine: "opencode", session_key: "opencode:x", outcome: "refused", reason: "not_bound" },
      ] } });
    }
    const id = path.split("/")[3];
    const row = rows.find((v) => v.id === id);
    if (req.method() === "POST" && path === "/api/variables") {
      const body = req.postDataJSON() as Row;
      const created = { id: "id-new", ...body, set: false, needs_reentry: false, agent_access: false, projects: [], created_at: clock, updated_at: ++clock };
      rows.push(created);
      return r.fulfill({ status: 201, json: created });
    }
    if (!row) return r.fulfill({ status: 404, json: { detail: "unknown" } });
    if (req.method() === "PUT" && path.endsWith("/file")) {
      const raw = req.postDataBuffer()?.toString("latin1") ?? "";
      expect(req.headers()["content-type"]).toContain("multipart/form-data");
      const name = /filename="([^"]+)"/.exec(raw)?.[1] ?? "";
      Object.assign(row, { set: true, file: { name, size: KEY_BYTES.length }, updated_at: ++clock });
      return r.fulfill({ json: row });
    }
    if (req.method() === "PATCH") {
      const body = req.postDataJSON() as Row;
      if ("agent_access" in body) row.agent_access = body.agent_access;
      Object.assign(row, { updated_at: ++clock });
      return r.fulfill({ json: row });
    }
    if (req.method() === "PUT" && path.endsWith("/projects")) {
      row.projects = (req.postDataJSON() as { project_ids: string[] }).project_ids;
      Object.assign(row, { updated_at: ++clock });
      return r.fulfill({ json: row });
    }
    return r.fulfill({ status: 405, json: { detail: "unexpected" } });
  });
  return { calls, rows };
}

async function noHorizontalOverflow(page: Page) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(over).toBeLessThanOrEqual(1);
}

for (const theme of ["dark", "light"] as const) {
  test(`secrets: list, editor, touch targets and no overflow (${theme})`, async ({ page }) => {
    await boot(page, theme);
    await page.goto("/library/variables");
    const fileRow = page.getByRole("article", { name: "asc_api_key" });
    await expect(fileRow.getByText("AuthKey_7F3K2L9QXA.p8")).toBeVisible(); // case kept, not upper-cased
    await expect(fileRow.getByText("Stored · hidden · 257 B")).toBeVisible();
    await expect(fileRow.getByText("Agents")).toBeVisible();
    await expect(page.getByRole("article", { name: "forgejo_token" }).getByText("Agents")).toHaveCount(0);
    await noHorizontalOverflow(page);

    const channels = page.getByText("How agents reach secrets");
    expect((await channels.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await channels.click();
    const list = page.getByRole("list", { name: "How each agent reaches secrets" });
    await expect(list.getByRole("listitem").filter({ hasText: "Claude Code" })).toContainText("Secrets tool");
    await expect(list.getByRole("listitem").filter({ hasText: "Gemini CLI" })).toContainText("Helper command");
    await noHorizontalOverflow(page);

    await fileRow.getByRole("button", { name: "Edit asc_api_key" }).click();
    const sw = page.getByRole("switch");
    await expect(sw).toHaveAttribute("aria-checked", "true");
    await expect(page.getByText(/Agents in Atlas iOS can ask the secrets tool/)).toBeVisible();
    await expect(page.getByText(/permission bypass can read the copy/)).toBeVisible();
    await expect(page.getByRole("link", { name: /open session/ })).toHaveAttribute("href", "/s/claude/release-24");
    await expect(page.getByText(/refused · not bound/)).toBeVisible();
    for (const control of [sw, page.getByRole("button", { name: /Replace file/ }), page.getByRole("checkbox", { name: "Atlas iOS" })]) {
      const box = await (await control.evaluateHandle((el) => (el as HTMLElement).closest("label") ?? el)).asElement()!.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    await noHorizontalOverflow(page);
    // The stored file's content never reaches the page.
    expect(await page.content()).not.toContain("E2ESECRET");
    await sw.click();
    await expect(page.getByText("Agent access · off")).toBeVisible();
    await expect(page.getByText(/No agent can request this secret/)).toBeVisible();
  });
}

test("a new secret file: type choice, upload, access, projects — saved in that order", async ({ page }) => {
  const { calls, rows } = await boot(page, "dark");
  await page.goto("/library/variables");
  await page.getByRole("button", { name: "New secret" }).click();
  await page.getByLabel("Name", { exact: true }).fill("prod_key");
  for (const label of ["Single line", "Multi-line · PEM, key, JSON", "File · upload"]) {
    const box = await page.getByRole("button", { name: label }).boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByRole("button", { name: "File · upload" }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "AuthKey_NEW.p8", mimeType: "application/octet-stream", buffer: Buffer.from(KEY_BYTES) });
  await expect(page.getByText("AuthKey_NEW.p8")).toBeVisible();
  await page.getByRole("switch").click();
  await page.getByRole("checkbox", { name: "Beta" }).check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText(/Saved “prod_key”/);
  const writes = calls.filter((c) => !c.startsWith("GET"));
  expect(writes).toEqual(["POST /", "PUT /id-new/file", "PATCH /id-new", "PUT /id-new/projects"]);
  const saved = rows.find((r) => r.id === "id-new")!;
  expect(saved).toMatchObject({ kind: "secret_file", agent_access: true, projects: ["beta"], file: { name: "AuthKey_NEW.p8" } });
  await noHorizontalOverflow(page);
});
