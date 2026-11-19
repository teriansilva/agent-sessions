/** Library → MCP servers (#2163 P1c): the index, the five-step wizard, and a server's page, on a
 *  real browser at desktop and phone size. The API is `mcpFixture.ts`'s stand-in (its header says
 *  why: a check needs a reachable MCP server); the pages, the router, the shell and every request
 *  they make are the real ones. What the requests SAID is asserted from the fake's call log, so a
 *  page that showed the right words but sent the wrong body still fails. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { MCP_OPERATIONS_API } from "../src/lib/apiPaths";
import { MCP_NEW_PATH, MCP_PATH, mcpServerPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";
import { emptyFake, mockMcp, record, seededFake, TOOLS, type Fake } from "./mcpFixture";

test.setTimeout(60_000);

const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

async function tallEnough(loc: Locator) {
  const box = await loc.boundingBox();
  expect(box, "laid out").not.toBeNull();
  expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
}

/** A switch draws a 44×24 track and grows its HIT area to 44 px tall on a phone (`.nj-switch::before`,
 *  nightjar.css): a tap 21 px above and below its centre must still land on it. */
async function hitTallEnough(loc: Locator) {
  const box = (await loc.boundingBox())!;
  const half = TOUCH_FLOOR / 2 - 1;
  const hits = await loc.evaluate(
    (el, p) => [p.y - p.half, p.y + p.half].map((y) => el.contains(document.elementFromPoint(p.x, y))),
    { x: box.x + box.width / 2, y: box.y + box.height / 2, half },
  );
  expect(hits, "the switch's hit area").toEqual([true, true]);
}

test.describe("index", () => {
  test("no servers: the empty state with Add, never a list", async ({ page }) => {
    await mockMcp(page, emptyFake());
    await page.goto(MCP_PATH);
    const empty = page.getByTestId("mcp-empty");
    await expect(empty).toContainText("No MCP servers yet");
    await expect(page.getByTestId("mcp-row")).toHaveCount(0);
    await empty.getByRole("link", { name: "Add MCP server" }).click();
    await expect(page).toHaveURL(new RegExp(`${MCP_NEW_PATH}$`));
  });

  test("a failed read says so in red with Retry; it is never the empty state", async ({ page }) => {
    const f = seededFake();
    f.listErrors = 1;
    await mockMcp(page, f);
    await page.goto(MCP_PATH);
    const alert = page.getByTestId("mcp-read-error");
    await expect(alert).toContainText("Couldn’t load MCP servers");
    await expect(alert).toContainText("the MCP server registry cannot be read");
    await expect(page.getByTestId("mcp-empty")).toHaveCount(0);
    await alert.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("mcp-row")).toHaveCount(3);
    await expect(alert).toHaveCount(0);
  });

  test("each row: source, tools, projects and its state in words; the whole row is the link", async ({ page }) => {
    const f = seededFake();
    await mockMcp(page, f);
    await page.goto(MCP_PATH);
    const rows = page.getByTestId("mcp-row");
    await expect(rows).toHaveCount(3);
    const tracker = rows.filter({ hasText: "Tracker" });
    await expect(tracker).toContainText("remote · mcp.example.com");
    await expect(tracker).toContainText("Authorization header");
    await expect(tracker).toContainText(`${TOOLS.length}tools`);
    await expect(tracker).toContainText("All projects");
    await expect(tracker.getByTestId("mcp-row-state")).toHaveText("Ready");
    await expect(tracker.getByTestId("mcp-row-state")).toHaveAttribute("data-tone", "up");

    const files = rows.filter({ hasText: "Files" });
    await expect(files).toContainText("npm · files-mcp-server 0.14.2");
    await expect(files).toContainText("3 of 3 projects");
    // Cedar has neither its own value nor the shared one bound: amber, it needs the operator.
    await expect(files.getByTestId("mcp-row-state")).toHaveText("Needs credentials in 1 project");
    await expect(files.getByTestId("mcp-row-state")).toHaveAttribute("data-tone", "att");

    const errors = rows.filter({ hasText: "Errors" });
    await expect(errors.getByTestId("mcp-row-state")).toHaveText("Check failing in Beta");
    await expect(errors.getByTestId("mcp-row-state")).toHaveAttribute("data-tone", "down");

    if (isPhone(page)) {
      await tallEnough(tracker);
      await tallEnough(page.getByTestId("mcp-add"));
    }
    await errors.click();
    await expect(page).toHaveURL(new RegExp(`${mcpServerPath(f.servers[2].id)}$`));
    await expect(page.getByTestId("mcp-server-page")).toHaveAttribute("data-server", "errors");
  });

  test("keyboard: Add and every row are reachable with Tab and open with Enter", async ({ page }) => {
    const f = seededFake();
    await mockMcp(page, f);
    await page.goto(MCP_PATH);
    await expect(page.getByTestId("mcp-row")).toHaveCount(3);
    const first = page.getByTestId("mcp-row").first();
    const add = page.getByTestId("mcp-add");
    let reachedAdd = false;
    let reachedRow = false;
    for (let i = 0; i < 40 && !reachedRow; i++) {
      await page.keyboard.press("Tab");
      if (await add.evaluate((el) => el === document.activeElement)) reachedAdd = true;
      if (await first.evaluate((el) => el === document.activeElement)) reachedRow = true;
    }
    expect(reachedAdd, "Add MCP server takes focus").toBe(true);
    expect(reachedRow, "the first row takes focus").toBe(true);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`${mcpServerPath(f.servers[0].id)}$`));
  });
});

test.describe("wizard", () => {
  test("remote: source → review → a new write-only credential → check → enable per project", async ({ page }) => {
    const f = emptyFake();
    // Beta keeps its OWN value for the credential the wizard is about to create; its check runs
    // when Enable is pressed, and this one fails, so Beta stays off and says why.
    f.failing.add("p_beta");
    await mockMcp(page, f);
    await page.goto(MCP_NEW_PATH);
    await expect(page.getByTestId("mcp-step-source")).toBeVisible();
    // Git is offered and not pickable yet.
    await expect(page.getByRole("radio", { name: /Git repository/ })).toBeDisabled();
    await page.getByLabel("Server URL").fill("https://mcp.example.com/mcp");
    await expect(page.getByLabel("Name", { exact: true })).toHaveValue("example");
    await page.getByLabel("Label (optional)").fill("Example");
    if (isPhone(page)) await tallEnough(page.getByRole("button", { name: "Next: Review" }));
    await page.getByRole("button", { name: "Next: Review" }).click();

    // Review: the URL host, nothing to install, the third-party caution.
    await expect(page.getByTestId("mcp-step-review")).toBeVisible();
    await expect(page.getByTestId("mcp-review-facts")).toContainText("mcp.example.com");
    await expect(page.getByTestId("mcp-review-facts")).toContainText("Nothing to install");
    await expect(page.getByTestId("mcp-caution")).toContainText("Third-party server");
    const created = f.calls.find((c) => c.method === "POST" && c.path === "/api/mcp/servers");
    expect(created?.body).toMatchObject({ name: "example", label: "Example", source: { kind: "remote", url: "https://mcp.example.com/mcp" } });
    expect(f.calls.some((c) => c.path.endsWith("/review") || c.path.endsWith("/install"))).toBe(false);
    const sid = f.servers[0].id;
    await page.getByRole("button", { name: "Next: Settings" }).click();

    // Settings: one credential, delivered as an Authorization header with a Bearer prefix.
    await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
    await page.getByTestId("mcp-add-setting").click();
    await page.getByLabel("Setting 1 name").fill("Authorization");
    await page.getByRole("radio", { name: "Credential" }).click();
    await expect(page.getByLabel("Variable name for Authorization")).toHaveValue("example_authorization");
    const secret = "test-only-not-a-real-token";
    await page.getByLabel("New credential value for Authorization").fill(secret);
    await expect(page.getByLabel("New credential value for Authorization")).toHaveAttribute("type", "password");
    await page.getByLabel("Prefix for Authorization").fill("Bearer ");
    await expect(page.getByTestId("mcp-setting-row")).toContainText("HTTP header");
    await expect(page.getByTestId("mcp-setting-row")).toContainText("Authorization: Bearer …");
    // Beta already holds its own value (as if set before): the fake records it now.
    f.own[sid] = { p_beta: ["example_authorization"] };
    await page.getByRole("button", { name: "Save & check" }).click();

    // Check: the stored secret went to Variables once; the server names it; the shared check ran.
    await expect(page.getByTestId("mcp-step-check")).toBeVisible();
    const report = page.getByTestId("mcp-check-report");
    await expect(report).toHaveAttribute("data-outcome", "passed");
    await expect(report.locator("li[data-step]")).toHaveCount(4);
    await expect(report.locator('li[data-step="handshake"]')).toContainText("protocol 2025-06-18");
    await report.getByRole("button", { name: "Show list" }).click();
    await expect(report.getByRole("list", { name: "Tools" }).getByRole("listitem")).toHaveText(TOOLS);
    const stored = f.calls.filter((c) => c.method === "POST" && c.path === "/api/variables");
    expect(stored).toHaveLength(1);
    expect(stored[0].body).toMatchObject({ name: "example_authorization", kind: "secret", value: secret });
    const patched = f.calls.find((c) => c.method === "PATCH" && c.path === `/api/mcp/servers/${sid}`);
    expect(patched?.body).toMatchObject({
      settings: [{ key: "Authorization", kind: "credential", variable: "example_authorization", prefix: "Bearer " }],
    });
    expect(JSON.stringify(patched?.body)).not.toContain(secret);
    expect(f.calls.find((c) => c.path.endsWith("/check"))?.body).toEqual({ scope: "shared" });
    // The typed value is gone from the page once stored.
    expect(await page.locator("body").evaluate((b) => b.innerHTML)).not.toContain(secret);
    await page.getByRole("button", { name: "Next: Enable" }).click();

    // Enable: Atlas and Cedar use the shared value (bound on Enable); Beta's own value is checked.
    await expect(page.getByTestId("mcp-step-enable")).toBeVisible();
    const rows = page.getByTestId("mcp-enable-row");
    await expect(rows).toHaveCount(3);
    await expect(rows.filter({ hasText: "Atlas" })).toHaveAttribute("data-action", "bind");
    await expect(rows.filter({ hasText: "Beta" })).toHaveAttribute("data-action", "check");
    await expect(rows.filter({ hasText: "Beta" })).toContainText("runs on Enable");
    await page.getByRole("button", { name: "Enable", exact: true }).click();
    await expect(rows.filter({ hasText: "Beta" })).toContainText("Check failed: 401 Unauthorized");
    await expect(rows.filter({ hasText: "Beta" })).toContainText("Left off.");
    const binds = f.calls.filter((c) => c.method === "PUT" && c.path.startsWith("/api/variables/"));
    expect(binds).toHaveLength(1);
    expect(binds[0].body).toMatchObject({ project_ids: ["p_atlas", "p_cedar"] });
    expect(f.calls.filter((c) => c.path.endsWith("/check")).map((c) => c.body)).toEqual([
      { scope: "shared" },
      { scope: "p_beta" },
    ]);
    const put = f.calls.find((c) => c.method === "PUT" && c.path === `/api/mcp/servers/${sid}/projects`);
    expect(put?.body).toMatchObject({ projects: ["p_atlas", "p_cedar"] });
    await page.getByRole("link", { name: "Open the server page" }).click();
    await expect(page.getByTestId("mcp-server-state")).toHaveText("Ready");
  });

  test("a failed shared check offers Edit credentials and Run check again", async ({ page }) => {
    const f = emptyFake();
    f.failing.add("shared");
    await mockMcp(page, f);
    await page.goto(MCP_NEW_PATH);
    await page.getByLabel("Server URL").fill("https://mcp.example.com/mcp");
    await page.getByRole("button", { name: "Next: Review" }).click();
    await page.getByRole("button", { name: "Next: Settings" }).click();
    await page.getByRole("button", { name: "Save & check" }).click();
    const report = page.getByTestId("mcp-check-report");
    await expect(report).toHaveAttribute("data-outcome", "failed");
    await expect(report).toContainText("Step 3 of 3: Handshake.");
    await expect(report.locator('li[data-step="handshake"]')).toContainText("failed");
    f.failing.clear();
    await report.getByRole("button", { name: "Run check again" }).click();
    await expect(report).toHaveAttribute("data-outcome", "passed");
    await page.getByRole("button", { name: "Back" }).click();
    await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
  });

  /** Register an npm package server and land on Review (whose operation starts by itself). */
  async function toReview(page: Page) {
    await page.goto(MCP_NEW_PATH);
    await page.getByRole("radio", { name: /npm package/ }).check();
    await page.getByLabel("Package", { exact: true }).fill("files-mcp-server");
    await page.getByLabel("Version", { exact: true }).fill("0.14.2");
    await page.getByRole("button", { name: "Next: Review" }).click();
  }
  const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const posts = (f: Fake, sub: "/review" | "/install") =>
    f.calls.filter((c) => c.method === "POST" && c.path.endsWith(sub)).map((c) => c.body as Record<string, string>);
  const polls = (f: Fake, id: string) => f.calls.filter((c) => c.method === "GET" && c.path === `${MCP_OPERATIONS_API}/${id}`);

  test("a package: Review and Install are operations the page polls, and Install is what installs", async ({ page }) => {
    const f = emptyFake();
    f.opPolls = 2; // each operation answers `running` twice before it ends
    await mockMcp(page, f);
    await page.goto(MCP_NEW_PATH);
    await page.getByRole("radio", { name: /npm package/ }).check();
    await page.getByLabel("Package", { exact: true }).fill("files-mcp-server");
    await page.getByLabel("Version", { exact: true }).fill("latest");
    await page.getByRole("button", { name: "Next: Review" }).click();
    await expect(page.getByText("An exact version, e.g. 1.4.2.")).toBeVisible();
    await page.getByLabel("Version", { exact: true }).fill("0.14.2");
    await page.getByRole("button", { name: "Next: Review" }).click();

    // Review: started as an operation (a client-made UUID), progress while it runs, then the facts.
    await expect(page.getByTestId("mcp-op-note")).toContainText("a minute or two");
    const facts = page.getByTestId("mcp-review-facts");
    await expect(facts).toContainText("registry.npmjs.org");
    await expect(facts).toContainText("postinstall are skipped");
    await expect(page.getByTestId("mcp-caution")).toContainText("runs as your user");
    const [review] = posts(f, "/review");
    expect(Object.keys(review)).toEqual(["request_id"]);
    expect(review.request_id).toMatch(UUID);
    expect(polls(f, review.request_id).length).toBeGreaterThanOrEqual(2);
    expect(posts(f, "/install")).toEqual([]);

    // Install: its own operation, naming the review; the step moves on only when it is done.
    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByRole("button", { name: "Installing…" })).toBeVisible();
    await expect(page.getByTestId("mcp-op-note")).toContainText("can take a few minutes");
    await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
    const [install] = posts(f, "/install");
    expect(install).toEqual({ request_id: expect.stringMatching(UUID), review_id: `rv_${review.request_id.slice(0, 8)}` });
    expect(install.request_id).not.toBe(review.request_id);
    expect(polls(f, install.request_id).length).toBeGreaterThanOrEqual(2);
    expect(f.ops[install.request_id].state).toBe("done");
    // A package's settings are environment variables.
    await page.getByTestId("mcp-add-setting").click();
    await page.getByLabel("Setting 1 name").fill("FILES_TOKEN");
    await expect(page.getByTestId("mcp-setting-row")).toContainText("env variable");
  });

  test("a failed install shows its words and stays on Review; Install again is a fresh operation", async ({ page }) => {
    const f = emptyFake();
    const words = "npm ci failed: integrity check of files-mcp-server@0.14.2 did not match the lock";
    f.installFails = words;
    await mockMcp(page, f);
    await toReview(page);
    await expect(page.getByTestId("mcp-review-facts")).toBeVisible();
    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByTestId("mcp-wizard-error")).toHaveText(words);
    await expect(page.getByTestId("mcp-step-review")).toBeVisible();
    expect(f.servers[0].installed).toBeNull();

    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
    const ids = posts(f, "/install").map((b) => b.request_id);
    expect(ids).toHaveLength(2);
    expect(ids[1]).not.toBe(ids[0]);
  });

  test("a 409 (another review or install of this server is running) shows its words", async ({ page }) => {
    const f = emptyFake();
    await mockMcp(page, f);
    await toReview(page);
    await expect(page.getByTestId("mcp-review-facts")).toBeVisible();
    // Another tab started an install of this server that is still running.
    const other = "0b0b0b0b-0000-4000-8000-000000000001";
    f.ops[other] = {
      id: other,
      server: f.servers[0].id,
      kind: "install",
      state: "running",
      created_at: 0,
      updated_at: 0,
      result: null,
      error: null,
      polls: Infinity,
    };
    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByTestId("mcp-wizard-error")).toHaveText("this server is being reviewed or installed");
    await expect(page.getByTestId("mcp-step-review")).toBeVisible();
    // Once it ends, Install goes through.
    f.ops[other].state = "done";
    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
  });

  test("an operation the app forgot (a restart) says so, and Review again starts afresh", async ({ page }) => {
    const f = emptyFake();
    f.opPolls = Infinity;
    await mockMcp(page, f);
    await toReview(page);
    await expect.poll(() => posts(f, "/review").length).toBe(1);
    const first = posts(f, "/review")[0].request_id;
    await expect.poll(() => polls(f, first).length).toBeGreaterThanOrEqual(1);
    f.opPolls = 1;
    delete f.ops[first];
    await expect(page.getByText("The operation was lost; try again.")).toBeVisible();
    await page.getByRole("button", { name: "Review again" }).click();
    await expect(page.getByTestId("mcp-review-facts")).toBeVisible();
    const ids = posts(f, "/review").map((b) => b.request_id);
    expect(ids).toHaveLength(2);
    expect(ids[1]).not.toBe(first);
  });
});

test.describe("server page", () => {
  async function open(page: Page, f: Fake, index: number, tab = "") {
    await mockMcp(page, f);
    await page.goto(`${mcpServerPath(f.servers[index].id)}${tab ? `?tab=${tab}` : ""}`);
    await expect(page.getByTestId("mcp-server-page")).toBeVisible();
  }

  test("Overview lists the tools of the newest passing check", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 0);
    await expect(page.getByTestId("mcp-server-state")).toHaveText("Ready");
    await expect(page.getByTestId("mcp-tools").getByRole("listitem")).toHaveText(TOOLS);
    await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  test("Projects: Own / Shared / Missing, the check, the switch and each row's actions", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 1, "projects");
    const row = (pid: string) => page.locator(`[data-testid="mcp-project-row"][data-project="${pid}"]`);
    await expect(row("p_atlas")).toContainText("Shared");
    await expect(row("p_atlas")).toContainText("Passed");
    await expect(row("p_atlas").getByRole("button", { name: "Use own credentials" })).toBeVisible();
    await expect(row("p_beta")).toContainText("Own");
    await expect(row("p_beta").getByRole("button", { name: "Edit own credentials" })).toBeVisible();
    await expect(row("p_cedar")).toContainText("Missing");
    await expect(page.getByTestId("mcp-switch-p_cedar")).toBeDisabled();
    await expect(row("p_cedar").getByRole("button", { name: "Add own credentials" })).toBeVisible();
    if (isPhone(page)) {
      await hitTallEnough(page.getByTestId("mcp-switch-p_atlas"));
      await tallEnough(row("p_atlas").getByRole("button", { name: "Run check" }));
      await tallEnough(page.getByRole("tab", { name: "Settings" }));
    }

    // Use shared for Cedar binds the library secret to it, then its check can run.
    await row("p_cedar").getByRole("button", { name: "Use shared" }).click();
    await expect(row("p_cedar")).toContainText("Shared");
    const bind = f.calls.find((c) => c.method === "PUT" && c.path === "/api/variables/v_files/projects");
    expect(bind?.body).toMatchObject({ project_ids: ["p_atlas", "p_cedar"] });

    // Switching Atlas off sends the explicit list without it.
    await page.getByTestId("mcp-switch-p_atlas").click();
    await expect(page.getByTestId("mcp-switch-p_atlas")).toHaveAttribute("aria-checked", "false");
    const put = f.calls.filter((c) => c.path.endsWith("/projects") && c.path.startsWith("/api/mcp/")).at(-1);
    expect(put?.body).toMatchObject({ projects: ["p_beta", "p_cedar"] });
  });

  test("own credentials are write-only, and a changed one asks for a new check (amber)", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 1, "projects");
    const atlas = page.locator('[data-testid="mcp-project-row"][data-project="p_atlas"]');
    await atlas.getByRole("button", { name: "Use own credentials" }).click();
    const dialog = page.getByRole("dialog");
    const field = dialog.getByLabel("files_token");
    await expect(field).toHaveAttribute("type", "password");
    await expect(field).toHaveValue("");
    await field.fill("test-only-atlas-value");
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(page.getByTestId("mcp-flash")).toContainText("Run its check");
    expect(f.calls.find((c) => c.path.endsWith("/projects/p_atlas/credentials"))?.body).toEqual({
      values: { files_token: "test-only-atlas-value" },
    });
    await expect(atlas).toContainText("Own");
    await expect(atlas).toHaveAttribute("data-check", "stale");
    await expect(atlas).toContainText("Credentials changed — check again");
    await atlas.getByRole("button", { name: "Run check" }).click();
    await expect(atlas).toHaveAttribute("data-check", "passed");
  });

  test("Settings offers a probe only from checked tools, with an explicit may-change-data box", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 0, "settings");
    const probe = page.getByTestId("mcp-probe");
    await probe.getByLabel("Tool", { exact: true }).selectOption("list_issues");
    await probe.getByLabel("Arguments (JSON)").fill('{"limit": 1}');
    await expect(probe.getByRole("checkbox", { name: /This call may change data/ })).not.toBeChecked();
    await page.getByTestId("mcp-settings-save").click();
    await expect(page.getByTestId("mcp-flash")).toContainText("Saved");
    const patch = f.calls.find((c) => c.method === "PATCH");
    expect(patch?.body).toMatchObject({
      probe: { tool: "list_issues", arguments: { limit: 1 }, confirmed_write: false },
      settings: [{ key: "Authorization", kind: "credential", variable: "tracker_token", prefix: "Bearer " }],
    });
  });

  test("Activity lists the checks newest first; ⋯ disables and removes", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 2, "activity");
    const items = page.getByTestId("mcp-activity").locator(":scope > li");
    await expect(items).toHaveCount(2);
    await expect(items.first()).toHaveAttribute("data-outcome", "failed");
    await expect(items.first()).toContainText("Beta");
    await expect(items.nth(1)).toContainText("Shared credentials");

    await page.getByTestId("mcp-server-menu").click();
    await page.getByRole("menuitem", { name: "Disable" }).click();
    await expect(page.getByTestId("mcp-server-state")).toContainText("Disabled");
    await expect(page.getByTestId("mcp-disabled")).toBeVisible();
    expect(f.calls.find((c) => c.method === "PATCH")?.body).toMatchObject({ enabled: false });

    await page.getByTestId("mcp-server-menu").click();
    await page.getByRole("menuitem", { name: "Remove…" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Its credentials stay in Variables");
    await dialog.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${MCP_PATH}$`));
    await expect(page.getByTestId("mcp-row")).toHaveCount(2);
    expect(f.calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  test("keyboard: the tabs move with the arrow keys", async ({ page }) => {
    const f = seededFake();
    await open(page, f, 0);
    await page.getByRole("tab", { name: "Overview" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: /^Projects/ })).toBeFocused();
    await expect(page).toHaveURL(/tab=projects/);
    await expect(page.getByTestId("mcp-project-row")).toHaveCount(3);
  });

  test("Settings: a not-installed package is reviewed and installed through polled operations", async ({ page }) => {
    const f = seededFake();
    f.servers[1].installed = null;
    await open(page, f, 1, "settings");
    const panel = page.getByTestId("mcp-install-panel");
    await panel.getByRole("button", { name: "Review" }).click();
    await expect(panel.getByTestId("mcp-review-facts")).toContainText("registry.npmjs.org");
    await panel.getByRole("button", { name: /Install/ }).click();
    await expect(page.getByTestId("mcp-flash")).toContainText("Installed.");
    await expect(page.getByTestId("mcp-install-panel")).toHaveCount(0);
    const sent = f.calls.filter((c) => c.method === "POST").map((c) => [c.path.split("/").pop(), Object.keys(c.body as object).sort()]);
    expect(sent).toEqual([
      ["review", ["request_id"]],
      ["install", ["request_id", "review_id"]],
    ]);
    expect(Object.values(f.ops).map((o) => o.state)).toEqual(["done", "done"]);
  });

  test("an unknown or malformed id says it isn't here", async ({ page }) => {
    const f = seededFake();
    await mockMcp(page, f);
    await page.goto(mcpServerPath("not-an-id"));
    await expect(page.getByTestId("mcp-server-missing")).toContainText("This MCP server isn’t here");
    await page.goto(mcpServerPath(record({ name: "gone" }).id));
    await expect(page.getByTestId("mcp-server-missing")).toBeVisible();
  });
});
