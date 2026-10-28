/** Setup installs your agents and starts API-first (#1739), in a real browser on desktop and mobile.
 *
 *  - The wizard's Agents step lists every catalog agent and runs the Add-agent flow INLINE: review,
 *    install, verify and enable a catalog agent without leaving setup, through the same guarded
 *    `/api/plugins/*` operations (CSRF on every write) as Settings → Agents — here under
 *    `auth_mode=none`, the relay install's mode.
 *  - The Launch step preselects a ready API client and creates its session through New session's
 *    structured create; a refused create keeps setup open with the server's reason.
 *  - New session links to the Add-agent flow.
 *
 *  Mocked API, the generated roster fixture (`e2e/roster.ts`). */
import { expect, test, type Page, type Request } from "@playwright/test";

import { DASHBOARD_PATH, NEW_SESSION_PATH } from "../src/lib/routes";
import type { PluginCatalog, PluginGeneration, PluginOperation, PluginReview } from "../src/types/plugins";
import { ROSTER } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

const AGENT = "sample-agent";
const REVIEW: PluginReview = {
  id: "e51cddbe-23e6-42bf-a994-51e08e9b087f", plugin_id: AGENT, source: "signed", sequence: 9,
  digest: "a".repeat(64), recipe_digest: "b".repeat(64), adopted_path: null, adopted_sha256: null,
  expires_at: 2_000_000_000,
  entry: {
    manifest: {
      identity: { id: AGENT, label: "Sample agent", version: "1.2.3", publisher: "Example" },
      runtime: { kind: "pty" }, install: { kind: "tarball" }, signin: { kind: "none" },
    },
    recipe: { artifacts: [{ url: "https://example.com/sample-1.2.3.tar.gz", sha256: "c".repeat(64), destination: "." }] },
  },
};
const LIVE_AGENT = {
  id: AGENT, present: true, supports_new: true, supports_seed_start: false, seed_reason: null,
  bin: "/home/u/.local/share/battlelab/agents/sample-agent/bin/sample", label: "Sample agent",
  kind: "agent", runtime: "pty",
  display: { name: AGENT, badge: "sa", accent: "slate", id_prefix: null, order: 900 },
  session_id: { mint: "pinned" },
};
const ready = (id: string) => {
  const row = ROSTER.engines.find((e) => e.id === id)!;
  return { ...row, present: true, supports_new: true, api: { ...(row.api as object), unavailable_reason: null } };
};

interface Opts {
  /** Engines the server says can start (`new_session_engines`). */
  startable?: string[];
  /** Roster rows replaced by a READY API client. */
  readyApi?: string[];
  /** The structured create's answer. */
  create?: { status: number; json: unknown };
  /** The catalog agent has a sign-in command (a temporary terminal). */
  signin?: boolean;
  /** Hold the structured create's answer until `release()` is called. */
  holdCreate?: boolean;
  /** The install operation's state (`running` = still on the server). */
  installState?: PluginOperation["state"];
}

async function mockApp(page: Page, opts: Opts = {}) {
  const review = structuredClone(REVIEW);
  if (opts.signin) review.entry.manifest.signin = { kind: "cli-subcommand", subcommand: "login" };
  const gen: PluginGeneration = {
    id: "", review, required_checks: ["binary", "version", "new"], verification: null,
  };
  const data: PluginCatalog = {
    feed: { state: "ready", sequence: 9, error: null },
    catalog: [
      { manifest: review.entry.manifest, digest: REVIEW.recipe_digest, source: "signed", installable: true },
      // A catalog agent this host cannot install: still a row, with its reason (Hermes 7251).
      { manifest: { identity: { id: "blocked-agent", label: "Blocked agent", publisher: "Example", version: "2.0.0" }, runtime: { kind: "pty" } },
        digest: "e".repeat(64), source: "signed", installable: false, reason: "not offered for this platform" },
    ],
    plugins: [], operations: [], roster_generation: 1, roster_revision: null,
  };
  const writes: { path: string; body: Record<string, unknown>; csrf: string | null }[] = [];
  let enabled = false;
  const startable = opts.startable ?? ["claude"];

  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "test-csrf", auth_mode: "none", username: null, terminal_backend: "ws",
        new_session_engines: [...startable, ...(enabled ? [AGENT] : [])],
        onboarded: false,
        agent_defaults: { bypass: true },
      },
    }),
  );
  await page.route("**/api/engines", (r) => {
    const engines = ROSTER.engines.map((e) => (opts.readyApi?.includes(e.id) ? ready(e.id) : e));
    return r.fulfill({ json: { engines: enabled ? [...engines, LIVE_AGENT] : engines, problems: [] } });
  });
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }] } }),
  );
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) =>
    r.fulfill({ json: { path: "/home/u", home: "/home/u", dirs: [] } }),
  );
  await page.route("**/api/ai-review/providers", (r) =>
    r.fulfill({ json: { providers: [{ id: "custom", label: "Custom (OpenAI-compatible)", group: "custom", kind: "openai-chat", base_url: "", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" }] } }),
  );
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  const op = (id: string, kind: PluginOperation["kind"], state: PluginOperation["state"]): PluginOperation => ({
    id, plugin_id: AGENT, kind, state, created_at: 1, updated_at: 1,
    generation_id: gen.id || id, review: kind === "install" ? gen.review : null,
    review_digest: gen.review.digest, error: null,
  });
  await page.route("**/api/plugins**", async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === "GET") {
      if (path === "/api/plugins") return r.fulfill({ json: data });
      const found = data.operations.find((o) => path.endsWith(o.id));
      return r.fulfill({ status: found ? 200 : 409, json: found ?? { detail: "plugin operation not found" } });
    }
    const body = (req.postDataJSON() ?? {}) as Record<string, unknown>;
    writes.push({ path, body, csrf: await req.headerValue("x-csrf-token") });
    if (path.endsWith("/review")) return r.fulfill({ json: gen.review });
    if (path.endsWith("/reload")) return r.fulfill({ json: { generation: 2 } });
    const kind = path.split("/").at(-1) as PluginOperation["kind"];
    const id = body.request_id as string;
    if (kind === "install") {
      gen.id = id;
      data.plugins = [{ id: AGENT, active: null, candidate: id, enabled: false, generations: [gen] }];
    }
    const result = op(id, kind,
      kind === "install" ? opts.installState ?? "installed" : kind === "signin" ? "ready" : kind === "verify" ? "verified" : "complete");
    if (kind === "verify") {
      gen.verification = { digest: gen.review.digest, checked_at: 2,
        results: gen.required_checks.map((check) => ({ check, passed: true, detail: "Real check passed." })) };
    }
    if (kind === "activate") {
      data.plugins[0].enabled = true;
      data.plugins[0].active = gen.id;
      data.plugins[0].candidate = gen.id;
      enabled = true;
    }
    data.operations.push(result);
    return r.fulfill({ json: result });
  });
  const creates: Request[] = [];
  let release = () => {};
  const held = new Promise<void>((r) => (release = r));
  await page.route("**/api/structured/sessions", async (r) => {
    creates.push(r.request());
    if (opts.holdCreate) await held;
    const answer = opts.create ?? { status: 200, json: { session_key: "codex-api:made-1" } };
    return r.fulfill({ status: answer.status, json: answer.json });
  });
  const starts: string[] = [];
  await page.route(/\/api\/structured\/sessions\/[^/]+\/start$/, (r) => {
    starts.push(r.request().url());
    return r.fulfill({ json: {} });
  });
  const sockets = { opened: 0, closed: 0 };
  await page.routeWebSocket(/\/ws\/plugins\/signin\//, (ws) => {
    sockets.opened += 1;
    const current = data.operations.find((o) => ws.url().endsWith(o.id));
    if (current) current.state = "running";
    ws.send(Buffer.from("Open https://example.com/device and enter the code\r\n"));
    ws.onClose(() => {
      sockets.closed += 1;
      if (current) current.state = "interrupted";
    });
  });
  const prefs: Record<string, unknown>[] = [];
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() === "POST") prefs.push(r.request().postDataJSON() as Record<string, unknown>);
    return r.fulfill({ json: {} });
  });
  return { data, writes, creates, prefs, sockets, starts, release };
}

async function openSetup(page: Page) {
  await page.goto(DASHBOARD_PATH, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(dialog.getByRole("heading", { name: "Welcome, Commander." })).toBeVisible();
  await dialog.getByRole("button", { name: /get started/i }).click(); // → security
  await dialog.getByRole("button", { name: /skip.*continue/i }).click(); // → agents (login off)
  await expect(dialog.getByText("claude", { exact: true })).toBeVisible();
  return dialog;
}

async function toLaunch(page: Page) {
  const dialog = await openSetup(page);
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → ai
  await dialog.getByRole("button", { name: /i'll do this later/i }).click(); // → project
  await dialog.getByRole("button", { name: /^next$/i }).click(); // → tour
  await dialog.getByRole("button", { name: /^skip$/i }).click(); // → launch
  await expect(dialog.getByRole("heading", { name: "Start your first session" })).toBeVisible();
  return dialog;
}

async function noHScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "page must not scroll horizontally").toBeLessThanOrEqual(1);
}

test("setup installs and enables a catalog agent inline, through the guarded operations (#1739)", async ({ page }) => {
  const m = await mockApp(page);
  const dialog = await openSetup(page);
  const row = dialog.locator(`[data-agent="${AGENT}"]`);
  await expect(row).toContainText(/not installed/i);
  await expect(row).toContainText("Terminal only");
  const blocked = dialog.locator('[data-agent="blocked-agent"]');
  await expect(blocked).toContainText("Unavailable");
  await expect(blocked).toContainText("not offered for this platform");
  await expect(blocked.getByRole("button")).toHaveCount(0);
  const install = row.getByRole("button", { name: "Install: Sample agent" });
  expect((await install.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await install.click();

  const flow = dialog.getByRole("region", { name: "Set up Sample agent" });
  // Fixed on this agent: no catalog picker, the review comes first.
  await expect(flow.getByRole("combobox")).toHaveCount(0);
  await flow.getByRole("button", { name: "Review installation" }).click();
  await expect(flow.getByRole("heading", { name: "Review this installation" })).toBeVisible();
  await expect(flow.getByText("Signed source · Example")).toBeVisible();
  await flow.getByRole("button", { name: "Install", exact: true }).click();
  await expect(flow.getByRole("heading", { name: "Connect your account" })).toBeVisible();
  await flow.getByRole("button", { name: "Continue to verification" }).click();
  await flow.getByRole("button", { name: "Verify installation" }).click();
  await flow.getByRole("button", { name: "Enable agent" }).click();

  // Enabled: the flow closes, the roster reloads, and the agent is Ready — still in setup.
  await expect(flow).toHaveCount(0);
  await expect(row).toContainText("Ready");
  await expect(dialog.getByRole("heading", { name: "Your agents" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${DASHBOARD_PATH}$`));
  // The same server operations, each a CSRF-guarded write, in order — under auth_mode=none too.
  expect(m.writes.map((w) => w.path)).toEqual([
    "/api/plugins/review", "/api/plugins/install", "/api/plugins/verify", "/api/plugins/activate", "/api/plugins/reload",
  ]);
  expect(m.writes.every((w) => w.csrf === "test-csrf")).toBe(true);
  expect(m.writes[1].body).toMatchObject({ review_id: REVIEW.id, digest: REVIEW.digest, confirm_local: false });
  // A first installation has nothing to replace, and its enable is still bound to the state read (#1741).
  expect(m.writes[3].body).toMatchObject({ expected_active: null, expected_revision: null });
  await noHScroll(page);
  if (process.env.SHOTS_1739) await page.screenshot({ path: `${process.env.SHOTS_1739}/agents-${test.info().project.name}.png`, fullPage: true });
});

test("Launch preselects a ready API client and creates its session through the structured create (#1739)", async ({ page }) => {
  const m = await mockApp(page, { startable: ["claude", "codex-api"], readyApi: ["codex-api"] });
  const dialog = await toLaunch(page);
  const picker = dialog.getByRole("radiogroup", { name: "Agent" });
  await expect(picker.getByRole("radio", { name: /codex — api/i })).toBeChecked();
  // The stored default skips: said before Launch (no prompts AND no sandbox), as New session does.
  await expect(dialog.getByTestId("setup-api-bypass-warning")).toContainText("without its sandbox");
  // Terminal stays one tap away; an API client that can't start is listed with its reason.
  await expect(picker.getByRole("radio", { name: /claude code/i })).not.toBeChecked();
  await expect(picker.getByTestId("setup-launch-unready").first()).toContainText(/not ready: .*or later is required/i);
  const row = picker.locator("label", { has: page.getByRole("radio", { name: /claude code/i }) });
  expect((await row.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await noHScroll(page);
  if (process.env.SHOTS_1739) await page.screenshot({ path: `${process.env.SHOTS_1739}/launch-${test.info().project.name}.png`, fullPage: true });

  await dialog.getByRole("button", { name: /launch session/i }).click();
  await expect(page).toHaveURL(/\/s\/codex-api\/made-1$/);
  expect(m.creates).toHaveLength(1);
  // The stored default skips (config `agent_defaults.bypass: true`): a skip create, then its start.
  expect(m.creates[0].postDataJSON()).toMatchObject({ engine: "codex-api", cwd: "/home/u/battlelab", bypass: true });
  await expect.poll(() => m.starts.length).toBe(1);
  expect(m.prefs.some((p) => p.onboarded === true)).toBe(true);
});

test("a refused API create keeps setup open with the server's reason, and does not complete setup (#1739)", async ({ page }) => {
  const m = await mockApp(page, {
    startable: ["claude", "codex-api"],
    readyApi: ["codex-api"],
    create: { status: 409, json: { detail: "codex is signed out" } },
  });
  const dialog = await toLaunch(page);
  await dialog.getByRole("button", { name: /launch session/i }).click();
  await expect(dialog.getByTestId("setup-launch-error")).toContainText("codex is signed out");
  await expect(dialog.getByRole("heading", { name: "Start your first session" })).toBeVisible();
  expect(m.prefs.some((p) => p.onboarded === true)).toBe(false);
  // Terminal is one tap away.
  await dialog.getByRole("radio", { name: /claude code/i }).check();
  await dialog.getByRole("button", { name: /launch session/i }).click();
  await expect(page).toHaveURL(/\/s\/claude\//);
});

test("New session links to the Add-agent flow (#1739)", async ({ page }) => {
  await mockApp(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { csrf: "test-csrf", auth_mode: "none", username: null, terminal_backend: "ws", new_session_engines: ["claude"], onboarded: true } }),
  );
  await page.goto(NEW_SESSION_PATH, { waitUntil: "domcontentloaded" });
  const add = page.getByTestId("new-session-add-agent");
  await expect(add).toBeVisible();
  expect((await add.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await add.click();
  await expect(page).toHaveURL(/\/settings\/agents\/setup\/new/);
  await expect(page.getByRole("heading", { name: "Choose an agent" })).toBeVisible();
});

test("a live inline sign-in is never closed silently: Close and closing setup ask, Next / Back keep it (#1739)", async ({ page }) => {
  const m = await mockApp(page, { signin: true });
  const dialog = await openSetup(page);
  await dialog.locator(`[data-agent="${AGENT}"]`).getByRole("button", { name: "Install: Sample agent" }).click();
  const flow = dialog.getByRole("region", { name: "Set up Sample agent" });
  await flow.getByRole("button", { name: "Review installation" }).click();
  await flow.getByRole("button", { name: "Install", exact: true }).click();
  await flow.getByRole("button", { name: "Start sign-in" }).click();
  const terminal = flow.getByLabel("Temporary vendor sign-in terminal");
  await expect(terminal).toBeVisible();
  await expect.poll(() => m.sockets.opened).toBe(1);

  // The flow's own Close asks; Stay keeps the terminal and its socket.
  await flow.getByRole("button", { name: "Close", exact: true }).click();
  const ask = page.getByRole("alertdialog", { name: "Leave sign-in?" });
  await expect(ask).toBeVisible();
  await ask.getByRole("button", { name: "Stay" }).click();
  await expect(terminal).toBeVisible();

  // Closing setup asks too (the wizard's ✕ and Esc); Stay keeps everything.
  await dialog.getByRole("button", { name: "Close", exact: true }).first().click();
  await expect(page.getByRole("alertdialog", { name: "Leave sign-in?" })).toBeVisible();
  await page.getByRole("alertdialog", { name: "Leave sign-in?" }).getByRole("button", { name: "Stay" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("alertdialog", { name: "Leave sign-in?" })).toBeVisible();
  await page.keyboard.press("Escape"); // Esc answers the confirm (Stay), not the wizard
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(dialog).toBeVisible();

  // Next and Back move between steps without unmounting the sign-in.
  await dialog.getByRole("button", { name: /^next$/i }).click();
  await expect(dialog.getByRole("heading", { name: "Set up your AI" })).toBeVisible();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(terminal).toBeVisible();
  expect(m.sockets).toEqual({ opened: 1, closed: 0 });

  // Confirmed: the terminal closes (and with it the sign-in).
  await flow.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("alertdialog", { name: "Leave sign-in?" }).getByRole("button", { name: "Leave sign-in" }).click();
  await expect(flow).toHaveCount(0);
  await expect.poll(() => m.sockets.closed).toBe(1);
});

test("an install still running on the server keeps its operation across Next / Back (#1739)", async ({ page }) => {
  const m = await mockApp(page, { installState: "running" });
  const dialog = await openSetup(page);
  await dialog.locator(`[data-agent="${AGENT}"]`).getByRole("button", { name: "Install: Sample agent" }).click();
  const flow = dialog.getByRole("region", { name: "Set up Sample agent" });
  await flow.getByRole("button", { name: "Review installation" }).click();
  await flow.getByRole("button", { name: "Install", exact: true }).click();
  await expect(flow.getByRole("region", { name: "Operation status" })).toContainText(/install · running/i);
  await dialog.getByRole("button", { name: /^next$/i }).click();
  await expect(dialog.getByRole("heading", { name: "Set up your AI" })).toBeVisible();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  // The same operation, read back — never a second install.
  await expect(flow.getByRole("region", { name: "Operation status" })).toContainText(/install · running/i);
  expect(m.writes.filter((w) => w.path === "/api/plugins/install")).toHaveLength(1);
});

test("a skip create that answers after setup was dismissed is never started and opens nothing (#1739)", async ({ page }) => {
  const m = await mockApp(page, { startable: ["claude", "codex-api"], readyApi: ["codex-api"], holdCreate: true });
  const dialog = await toLaunch(page);
  await dialog.getByRole("button", { name: /launch session/i }).click();
  await expect.poll(() => m.creates.length).toBe(1);
  await page.keyboard.press("Escape"); // dismiss setup while the create runs
  await expect(dialog).toHaveCount(0);
  m.release();
  // Give the late answer every chance to act, then prove it did nothing.
  await page.waitForResponse("**/api/structured/sessions");
  await page.evaluate(() => new Promise((r) => setTimeout(r, 500)));
  expect(m.starts).toEqual([]);
  await expect(page).toHaveURL(new RegExp(`${DASHBOARD_PATH}$`));
});

test("a failed operation opened from its row retries on THAT agent, never a picker for another (#1739)", async ({ page }) => {
  const m = await mockApp(page);
  // An install the server still holds: the row offers View operation (an operation-only query).
  const opId = "0b7f2c4e-9a51-4d3c-8f0e-2e6a1c9d7b11";
  m.data.operations.push({
    id: opId, plugin_id: AGENT, kind: "install", state: "running", created_at: 1, updated_at: 1,
    generation_id: opId, review: structuredClone(REVIEW), review_digest: REVIEW.digest, error: null,
  });
  const dialog = await openSetup(page);
  const row = dialog.locator(`[data-agent="${AGENT}"]`);
  await expect(row).toContainText("Setting up");
  m.data.operations[0].state = "failed";
  m.data.operations[0].error = "download checksum mismatch";
  await row.getByRole("button", { name: "View operation: Sample agent" }).click();
  const flow = dialog.getByRole("region", { name: "Set up Sample agent" });
  await expect(flow.getByText("download checksum mismatch")).toBeVisible();
  await flow.getByRole("button", { name: "Review a new attempt" }).click();
  // Back at the review step — still this agent: no catalog picker, no local manifest.
  await expect(flow.getByRole("heading", { name: "Choose an agent" })).toBeVisible();
  await expect(flow.getByRole("combobox")).toHaveCount(0);
  await expect(flow.getByRole("button", { name: "Local manifest" })).toHaveCount(0);
  await flow.getByRole("button", { name: "Review installation" }).click();
  await expect(flow.getByRole("heading", { name: "Review this installation" })).toBeVisible();
  expect(m.writes.filter((w) => w.path === "/api/plugins/review").map((w) => w.body.plugin_id)).toEqual([AGENT]);
});

test("a grouped API client's failed operation retries that API client, not its console agent (#1739)", async ({ page }) => {
  const m = await mockApp(page);
  const api = ROSTER.engines.find((e) => e.id === "codex-api")!;
  m.data.catalog.push({
    manifest: { identity: { id: "codex-api", label: String(api.label), publisher: "BattleLab", version: "1.0.0" },
      runtime: { kind: "api" }, api: { kind: "codex-app-server", source: "codex" } },
    digest: "f".repeat(64), source: "bundled", installable: true, included: true,
  });
  const opId = "5c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f";
  m.data.operations.push({
    id: opId, plugin_id: "codex-api", kind: "install", state: "running", created_at: 1, updated_at: 1,
    generation_id: opId, review: { ...structuredClone(REVIEW), plugin_id: "codex-api" }, review_digest: REVIEW.digest, error: null,
  });
  const dialog = await openSetup(page);
  // The API client's operation is shown on its console agent's row (one row per CLI, #1408).
  const row = dialog.locator('[data-agent="codex"]');
  await expect(row).toContainText("Setting up");
  m.data.operations.at(-1)!.state = "failed";
  await row.getByRole("button", { name: /^View operation: / }).click();
  const flow = row.getByRole("region");
  await flow.getByRole("button", { name: "Review a new attempt" }).click();
  await expect(flow.getByRole("combobox")).toHaveCount(0);
  await flow.getByRole("button", { name: "Review installation" }).click();
  await expect(flow.getByRole("heading", { name: "Review this installation" })).toBeVisible();
  // The retry is about the API client the operation was for — never the console agent.
  expect(m.writes.filter((w) => w.path === "/api/plugins/review").map((w) => w.body.plugin_id)).toEqual(["codex-api"]);
});
