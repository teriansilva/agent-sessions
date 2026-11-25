import { expect, test, type Locator, type Page } from "@playwright/test";
import { mockRoster, ROSTER } from "./roster";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

const ID = "d410d52a-4d13-4e9b-a4f6-5c4110378070";
const TURN = "d410d52a-4d13-4e9b-a4f6-5c4110378071";
type Json = Record<string, unknown>;
async function contrast(locator: Locator) {
  return locator.evaluate((el) => {
    // Computed tokens may use color(srgb …), not rgb(…); let the browser convert them.
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d")!;
    const rgb = (value: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = value;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const luminance = ([r, g, b]: number[]) => {
      const linear = (v: number) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
      return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
    };
    let bg = [0, 0, 0];
    for (let node: Element | null = el; node; node = node.parentElement) {
      const color = rgb(getComputedStyle(node).backgroundColor);
      if (color[3] === 255) { bg = color; break; }
    }
    const [a, b] = [luminance(rgb(getComputedStyle(el).color)), luminance(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}
async function setup(page: Page, engine: string, theme: string, requests: Json[]) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: {} }));
  await page.route("**/api/config", (r) => r.fulfill({ json: { csrf: "x", auth_mode: "none", theme, default_project: "/home/u/project", new_session_engines: [engine], unavailable_clients: [], overview_expanded: [], projects_hidden: [], terminal_backend: "ws" } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page);
  const state = {
    session_key: `${engine}:${ID}`, revision: 4, event_cursor: 4, cwd: "/home/u/project",
    state: "awaiting_approval", active_turn: TURN, read_only: null, omitted_turns: 0,
    model_requested: null, model_effective: null, native: { worker: "w1" },
    turns: [{ turn_id: TURN, operation_id: TURN, state: "awaiting_approval", text: "Review the bugs", reply: "", tools: [], text_truncated: false, reply_truncated: false }],
    pending_requests: requests,
  };
  const decisions: Json[] = [];
  let hold = false;
  await page.route("**/api/structured/sessions/**", async (r) => {
    if (r.request().url().endsWith("/events?after=4&limit=1") || r.request().url().includes("/events?")) {
      return r.fulfill({ json: { revision: state.revision, next_cursor: state.revision, events: [] } });
    }
    if (r.request().url().endsWith("/decisions")) {
      const body = r.request().postDataJSON() as Json; decisions.push(body);
      if (!hold) state.pending_requests = state.pending_requests.filter((x) => x.request_id !== body.request_id);
      state.revision++; state.event_cursor = state.revision;
      if (!state.pending_requests.length) { state.state = "idle"; state.turns[0].state = "completed"; state.turns[0].reply = "Done reviewing."; }
      return r.fulfill({ json: { decision: body.decision, handoff: "sent" } });
    }
    return r.fulfill({ json: state });
  });
  return { state, decisions, hold: () => { hold = true; } };
}
function command(id: string, text = "git status"): Json {
  return { request_id: id, turn_id: TURN, kind: "command", complete: true, choices: ["approve", "reject", "cancel"], payload_digest: `digest-${id}`, payload: { command: text, cwd: "/home/u/project", opaqueContext: { native: "retained in Details" } } };
}
test("OpenCode skip prompts is opt-in and starts only after create succeeds", async ({ page }) => {
  const { state } = await setup(page, "opencode-api", "dark", []);
  Object.assign(state, { state: "idle", active_turn: null, turns: [], bypass: true });
  const native = ROSTER.engines.find((e) => e.id === "opencode-api")!;
  await mockRoster(page, { overrides: { "opencode-api": {
    present: true, supports_new: true,
    api: { ...(native.api as Json), unavailable_reason: null },
  } } });
  const calls: string[] = [];
  await page.route("**/api/structured/sessions", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({ engine: "opencode-api", bypass: true });
    calls.push("create");
    await route.fulfill({ status: 201, json: { ...state, pending_start: true, native: null } });
  });
  await page.route("**/api/structured/sessions/**/start", (route) => {
    calls.push("start");
    return route.fulfill({ json: state });
  });
  await page.goto(NEW_SESSION_PATH);
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).not.toBeChecked();
  await skip.check();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/opencode-api/${ID}$`));
  expect(calls).toEqual(["create", "start"]);
  await expect(page.getByTestId("structured-bypass")).toBeVisible();
});
for (const theme of ["dark", "light"]) for (const engine of ["codex-api", "claude-api", "opencode-api"]) {
  test(`compact approval and complete Details: ${engine} ${theme}`, async ({ page, isMobile }, testInfo) => {
    if (isMobile) await page.setViewportSize({ width: 390, height: 844 });
    const request = command("7");
    if (engine === "claude-api") Object.assign(request, { kind: "Bash", payload: { tool_name: "Bash", input: { command: "git status" }, opaqueContext: { native: "retained in Details" } } });
    if (engine === "opencode-api") Object.assign(request, { kind: "execute", payload: { toolCall: { toolCallId: "c7", title: "bash", kind: "execute", rawInput: { command: "git status" } }, options: [], opaqueContext: { native: "retained in Details" } } });
    request.always = [{ id: "gfixture", scope: "session", label: engine === "opencode-api"
      ? "Remember OpenCode’s permission rules — matching patterns are not provided and may cover all uses of a tool · this worker and folder only, not saved; resets when the worker restarts"
      : "Allow this command for this session · not saved" }];
    await setup(page, engine, theme, [request]);
    await page.goto(`/s/${engine}/${ID}`);
    const card = page.getByTestId("structured-request");
    await expect(card).toContainText("git status");
    await card.screenshot({ path: testInfo.outputPath("compact-approval.png") });
    await testInfo.attach("compact-approval", { path: testInfo.outputPath("compact-approval.png"), contentType: "image/png" });
    const details = card.locator("details");
    await expect(details).not.toHaveAttribute("open");
    await expect(card.getByText(/retained in Details/)).not.toBeVisible();
    await details.locator("summary").click();
    await expect(card.getByText(/retained in Details/)).toBeVisible();
    await expect(card).toContainText("digest-7");
    await card.screenshot({ path: testInfo.outputPath("complete-details.png") });
    await testInfo.attach("complete-details", { path: testInfo.outputPath("complete-details.png"), contentType: "image/png" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    for (const button of await card.getByRole("button").all()) expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  });
}
test("after Allow a new OpenCode request names its tool and can also be allowed", async ({ page }) => {
  const first = { ...command("i:0"), kind: "execute", payload: { toolCall: { toolCallId: "c0", title: "git remote -v", kind: "execute", rawInput: { command: "git remote -v" } } } };
  const { state, decisions } = await setup(page, "opencode-api", "dark", [first]);
  await page.goto(`/s/opencode-api/${ID}`);
  await page.getByRole("button", { name: "Allow once", exact: true }).click();
  state.pending_requests = [{ ...command("i:1"), kind: "other", payload: { toolCall: { toolCallId: "c1", title: "forgejo_list_repo_labels", kind: "other", rawInput: {} }, toolContext: { rawInput: { owner: "superstatus.io", repo: "agent-sessions" } } } }];
  state.state = "awaiting_approval"; state.turns[0].state = "awaiting_approval"; state.revision++;
  const card = page.getByTestId("structured-request");
  await expect(card).toContainText("forgejo_list_repo_labels");
  await expect(card).toContainText("agent-sessions");
  await expect(card.getByRole("button", { name: "Allow once", exact: true })).toBeEnabled();
  await card.getByRole("button", { name: "Allow once", exact: true }).click();
  await expect(page.getByTestId("structured-request")).toHaveCount(0);
  expect(decisions.map((d) => d.request_id)).toEqual(["i:0", "i:1"]);
});
test("each queued request can be answered while another waits for agent acknowledgement", async ({ page }) => {
  const control = await setup(page, "codex-api", "dark", [command("7"), command("8", "git diff")]); control.hold();
  await page.goto(`/s/codex-api/${ID}`);
  const cards = page.getByTestId("structured-request");
  const before = (await cards.nth(1).boundingBox())!;
  await cards.nth(0).getByRole("button", { name: "Approve once", exact: true }).click();
  await expect(cards.nth(0).getByTestId("structured-deciding")).toContainText("waiting");
  expect(Math.abs((await cards.nth(1).boundingBox())!.y - before.y)).toBeLessThanOrEqual(2);
  await cards.nth(1).getByRole("button", { name: "Approve once", exact: true }).click();
  await expect.poll(() => control.decisions.length).toBe(2);
  await expect(page.getByTestId("structured-queue")).toContainText("2 requests waiting");
});

test("answering one request keeps the next action in place", async ({ page }) => {
  await setup(page, "codex-api", "dark", [command("7"), command("8", "git diff")]);
  await page.goto(`/s/codex-api/${ID}`);
  const first = page.getByTestId("structured-request").nth(0);
  const second = page.getByTestId("structured-request").nth(1);
  await expect(second).toBeVisible();
  const before = (await second.boundingBox())!;
  await first.getByRole("button", { name: "Approve once", exact: true }).click();
  await expect(page.getByTestId("structured-request")).toHaveCount(1);
  const after = (await page.getByTestId("structured-request").boundingBox())!;
  expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(2);
});

test("a held request does not accumulate closed cards across approval cycles", async ({ page }) => {
  const { state } = await setup(page, "codex-api", "dark", [command("held")]);
  await page.goto(`/s/codex-api/${ID}`);
  const cards = page.getByTestId("structured-request");
  await expect(cards).toHaveCount(1);
  const before = (await cards.first().boundingBox())!;
  for (let i = 0; i < 4; i++) {
    state.pending_requests.push(command(`cycle-${i}`, `git diff --stat ${i}`));
    state.revision++;
    await expect(cards).toHaveCount(2);
    await cards.last().getByRole("button", { name: "Approve once", exact: true }).click();
    await expect(cards).toHaveCount(1);
    await expect(page.getByText("Request closed", { exact: true })).toHaveCount(0);
    expect(Math.abs((await cards.first().boundingBox())!.y - before.y)).toBeLessThanOrEqual(2);
  }
});

test("settling a keyboard decision focuses the next pending approval", async ({ page }) => {
  await setup(page, "codex-api", "dark", [command("7"), command("8", "git diff")]);
  await page.goto(`/s/codex-api/${ID}`);
  const cards = page.getByTestId("structured-request");
  await cards.first().getByRole("button", { name: "Approve once", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(cards).toHaveCount(1);
  await expect(cards.getByRole("button", { name: "Approve once", exact: true })).toBeFocused();
});

test("leaving the approval queue retires its protected closed slots", async ({ page }) => {
  await setup(page, "codex-api", "dark", [command("7"), command("held", "git diff")]);
  await page.goto(`/s/codex-api/${ID}`);
  await page.getByTestId("structured-request").first().getByRole("button", { name: "Approve once", exact: true }).click();
  await expect(page.getByText("Request closed", { exact: true })).toHaveCount(1);
  await page.getByRole("textbox", { name: "Message Codex" }).click();
  await expect(page.getByText("Request closed", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("structured-request")).toHaveCount(1);
});

test("a delayed acknowledgement does not steal focus from the composer", async ({ page }) => {
  const control = await setup(page, "codex-api", "dark", [command("7"), command("8", "git diff")]);
  control.hold();
  await page.goto(`/s/codex-api/${ID}`);
  await page.getByTestId("structured-request").first().getByRole("button", { name: "Approve once", exact: true }).click();
  await expect(page.getByTestId("structured-deciding")).toContainText("waiting");
  const composer = page.getByRole("textbox", { name: "Message Codex" });
  await composer.focus();
  control.state.pending_requests = control.state.pending_requests.slice(1);
  control.state.revision++;
  await expect(page.getByTestId("structured-request")).toHaveCount(1);
  await expect(composer).toBeFocused();
});

for (const theme of ["dark", "light"]) test(`always choices and Details stay usable at 390px (${theme})`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const req = command("7", "git push --force origin main");
  req.always = Array.from({ length: 8 }, (_, i) => ({ id: `g${i}`, scope: "persistent", label: `Always allow git push and any command starting with it · saved to your policy, persists · RISKY: force-pushes over remote history (${i})` }));
  req.risk = { level: "risky", reasons: ["force-pushes over remote history"] };
  const { decisions } = await setup(page, "codex-api", theme, [req]);
  await page.goto(`/s/codex-api/${ID}`);
  const toggle = page.getByTestId("structured-always");
  await toggle.click();
  const choices = page.getByTestId("structured-always-option");
  await expect(choices.first()).toBeFocused();
  for (const target of [choices.first(), page.getByTestId("structured-risk"), page.locator("details > summary")]) {
    expect(await contrast(target)).toBeGreaterThanOrEqual(4.5);
  }
  const menu = (await choices.first().locator("../..").boundingBox())!;
  const composer = (await page.getByRole("form", { name: "Compose message" }).boundingBox())!;
  expect(menu.height).toBeLessThanOrEqual(390);
  expect(menu.y).toBeGreaterThanOrEqual(0);
  expect(menu.y + menu.height).toBeLessThanOrEqual(composer.y);
  await page.keyboard.press("Escape");
  await expect(toggle).toBeFocused();
  await expect(choices).toHaveCount(0);
  await toggle.click();
  await choices.last().click();
  expect(decisions[0]).toMatchObject({ decision: "always", grant: "g7" });
});
