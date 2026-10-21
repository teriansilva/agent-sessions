import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { agentPicker } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// #1505: starting an API session shows its page at once. The server is mocked with the new
// contract — create and /start answer once the worker is LAUNCHED, and the snapshot's `launch`
// view turns `ready` (or `failed`) by itself after a mocked 3 s. The skip start's own answer is
// slow here (it arrives after those 3 s, as today's /start did when it waited for ready), so a
// client that waits for it keeps the operator on New session; the page must not.
//
// On the client before #1505 both starts never show Starting…: a guarded create opens a pane that
// knows no launch view (Send live before the agent is ready), and a skip create stays on New
// session until /start answers — by then the session is ready. Desktop AND mobile.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);
const READY_AFTER_MS = 3_000;
const REASON = "the codex CLI exited during setup (exit 1: not logged in)";

type Json = Record<string, unknown>;

function snapshot(over: Json = {}): Json {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/home/u/proj",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: null, worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

async function setup(page: Page, bypass: boolean) {
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude", "codex-api"],
        unavailable_clients: [],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project: "/home/u/proj",
        overview_expanded: [],
        projects_hidden: [],
        theme: "dark",
        agent_defaults: { bypass },
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
}

/** The mocked server: `launched` is when the worker was launched (create for a guarded session,
 *  the /start's ARRIVAL for a skip one); readiness (or failure) follows `READY_AFTER_MS` later. */
async function serve(page: Page, opts: { bypass: boolean; fail?: boolean }) {
  let launched: number | null = null;
  const view = (): Json => {
    const base = opts.bypass ? { bypass: true } : {};
    if (launched === null) {
      return snapshot({ ...base, pending_start: true, native: { native_id: null, worker: null, background_active: false } });
    }
    if (Date.now() - launched < READY_AFTER_MS) {
      return snapshot({
        ...base,
        ...(opts.bypass ? { start_in_flight: true } : {}),
        launch: { gen: "w1", state: "starting", since: launched / 1000 },
      });
    }
    if (opts.fail) {
      return snapshot({
        ...base,
        ...(opts.bypass
          ? { pending_start: true, start_incomplete: true, start_failed: true, start_failed_reason: REASON }
          : {}),
        native: { native_id: null, worker: null, background_active: false },
        launch: { gen: "w1", state: "failed", reason: REASON, since: launched / 1000 + 3 },
      });
    }
    return snapshot({ ...base, native: { native_id: "n1", worker: "w1", background_active: false }, launch: { gen: "w1", state: "ready", since: 1 } });
  };
  await page.route("**/api/structured/sessions", async (r) => {
    if (!opts.bypass) launched = Date.now(); // answered at launch, not at ready
    await r.fulfill({ status: 201, json: view() });
  });
  await page.route(SESSION, async (r: Route) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") {
      return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    }
    if (sub === "/start") {
      launched = Date.now();
      await new Promise((done) => setTimeout(done, READY_AFTER_MS + 500)); // a slow answer
      return r.fulfill({ json: view() });
    }
    if (sub === "/stop") return r.fulfill({ json: { session_key: KEY, containment: "gone" } });
    if (sub === "") return r.fulfill({ json: view() });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

async function startFromNewSession(page: Page) {
  await page.goto(NEW_SESSION_PATH);
  await agentPicker(page).selectOption("codex-api");
  await page.getByRole("button", { name: /start session/i }).click();
}

const startButtons = (page: Page) => page.getByRole("button", { name: /^start( again| \(|$)/i });

for (const kind of ["guarded", "skip"] as const) {
  test(`${kind}: the session page opens at launch with Starting…, then turns ready by itself`, async ({ page }) => {
    const bypass = kind === "skip";
    await setup(page, bypass);
    await serve(page, { bypass });
    await startFromNewSession(page);
    // The page is there while the agent is still getting ready (and, for skip, while /start's
    // answer is still on its way): Starting… can only be seen before the mocked ready.
    await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`), { timeout: READY_AFTER_MS });
    const startingLine = page.getByTestId("structured-starting");
    await expect(startingLine).toBeVisible({ timeout: READY_AFTER_MS });
    await expect(startingLine).toContainText("Starting");
    const send = page.getByTestId("structured-send");
    await expect(send).toBeDisabled();
    await expect(send).toContainText("Starting…");
    await expect(startButtons(page)).toHaveCount(0);
    await expect(page.getByText(/nothing has run/i)).toHaveCount(0);
    // The composer takes a draft meanwhile; nothing is sent.
    const box = page.getByRole("textbox", { name: /message/i });
    await expect(box).toBeEnabled();
    await box.fill("written while it starts");
    // Ready arrives through the snapshot alone; the draft is still there when Send unlocks.
    await expect(startingLine).toHaveCount(0);
    await expect(send).toBeEnabled();
    await expect(send).toContainText("Send");
    await expect(box).toHaveValue("written while it starts");
    await expect(page.getByTestId("structured-worker")).toContainText("worker live");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

test("guarded: a create that never became ready shows its reason with New session and Discard", async ({
  page,
}) => {
  // Skip-by-default ON, and the operator chose guarded for this one (Hermes on #1518, 4).
  await setup(page, true);
  await serve(page, { bypass: false, fail: true });
  await page.goto(NEW_SESSION_PATH);
  await agentPicker(page).selectOption("codex-api");
  const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
  await expect(skip).toBeChecked();
  await skip.uncheck();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page.getByTestId("structured-starting")).toBeVisible({ timeout: READY_AFTER_MS });
  const banner = page.getByTestId("structured-create-failed");
  await expect(banner).toContainText(`Couldn’t start: ${REASON}`);
  await expect(banner.getByRole("button", { name: "New session" })).toBeVisible();
  await expect(banner.getByRole("button", { name: "Discard" })).toBeVisible();
  await expect(startButtons(page)).toHaveCount(0); // a guarded creation never offers Start again
  await expect(page.getByTestId("structured-worker")).toContainText("start failed");
  if (test.info().project.name === "mobile") {
    for (const name of ["New session", "Discard"]) {
      const box = await banner.getByRole("button", { name }).boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(43.5); // the 44px touch target
    }
  }
  await banner.getByRole("button", { name: "New session" }).click();
  await expect(page).toHaveURL(new RegExp(`${NEW_SESSION_PATH}$`));
  await expect(agentPicker(page).radio("codex-api")).toBeChecked();
  // The replacement is guarded too, whatever the stored default says.
  await expect(page.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
});

test("skip: a start that failed shows its reason with Start again and Discard", async ({ page }) => {
  await setup(page, true);
  await serve(page, { bypass: true, fail: true });
  await startFromNewSession(page);
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`), { timeout: READY_AFTER_MS });
  await expect(page.getByTestId("structured-starting")).toBeVisible({ timeout: READY_AFTER_MS });
  const banner = page.getByTestId("structured-pending-start");
  await expect(banner).toContainText(`Couldn’t start: ${REASON}`);
  await expect(banner).not.toContainText(/nothing has run/i);
  await expect(banner.getByRole("button", { name: "Start again" })).toBeEnabled();
  await expect(banner.getByRole("button", { name: "Discard" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "New session" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: /message/i })).toBeDisabled();
});

// Hermes on #1518 (round 2, 1): the guarded choice "New session" restores must survive the New
// project detour — wizard cancel, wizard finish and the browser's Back — with skip-by-default ON.
async function mockWizard(page: Page) {
  const projects: Json[] = [];
  await page.route(/\/api\/projects(\?.*)?$/, async (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: { projects } });
    const body = r.request().postDataJSON() as Record<string, string>;
    const p = {
      id: "p-new",
      name: body.name,
      color: body.color ?? "",
      folders: [body.default_folder],
      default_folder: body.default_folder,
      archived: false,
      created_at: 0,
      session_count: 0,
    };
    projects.push(p);
    return r.fulfill({ json: p });
  });
  await page.route(/\/api\/fs\/dirs(\?.*)?$/, (r) => {
    const path = new URL(r.request().url()).searchParams.get("path") || "/home/u";
    return r.fulfill({ json: { path, home: "/home/u", dirs: [] } });
  });
  await page.route("**/api/fs/mkdir", async (r) => {
    const body = r.request().postDataJSON() as { parent: string; name: string };
    await r.fulfill({ json: { path: `${body.parent}/${body.name}` } });
  });
  await page.route("**/api/playbooks", (r) => r.fulfill({ json: { playbooks: [], default: null, recovery_total: 0 } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
}

for (const detour of ["cancel", "finish", "back"] as const) {
  test(`guarded "New session" stays guarded through the New project wizard (${detour})`, async ({ page }) => {
    await setup(page, true); // skip-by-default ON
    await mockWizard(page);
    const creates: Json[] = [];
    await page.route(SESSION, (r) =>
      r.request().url().includes("/events")
        ? r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } })
        : r.fulfill({
            json: snapshot({
              native: { native_id: null, worker: null, background_active: false },
              launch: { gen: "w1", state: "failed", reason: REASON, since: 1 },
            }),
          }),
    );
    await page.route("**/api/structured/sessions", async (r) => {
      creates.push(r.request().postDataJSON() as Json);
      await r.fulfill({ status: 201, json: snapshot({ launch: { gen: "w2", state: "starting", since: 2 } }) });
    });
    await page.goto(`/s/codex-api/${ID}`);
    await page.getByTestId("structured-create-failed").getByRole("button", { name: "New session" }).click();
    const skip = page.getByRole("checkbox", { name: /skip permission prompts/i });
    await expect(skip).not.toBeChecked();
    await page.getByRole("link", { name: /new project/i }).click();
    await expect(page.getByRole("heading", { name: "Name the project", exact: true })).toBeVisible();
    if (detour === "back") {
      await page.goBack();
    } else if (detour === "cancel") {
      await page.getByRole("button", { name: "Cancel" }).click(); // a clean wizard: no question
    } else {
      await page.getByLabel("Project name").fill("Payments API");
      // Next through however many steps the wizard has, up to its review.
      const review = page.getByRole("heading", { name: "Review and create", exact: true });
      for (let i = 0; i < 8 && !(await review.isVisible()); i++) {
        await page.getByRole("button", { name: "Next", exact: true }).click();
      }
      await expect(review).toBeVisible();
      await page.getByRole("button", { name: "Create project" }).click();
      await page.getByRole("button", { name: "Start a session" }).click();
    }
    await expect(page.getByRole("heading", { name: "Start a new session" })).toBeVisible();
    await expect(agentPicker(page).radio("codex-api")).toBeChecked();
    await expect(skip).not.toBeChecked();
    await page.getByRole("button", { name: /start session/i }).click();
    await expect.poll(() => creates.length).toBe(1);
    expect(creates[0].bypass ?? false).toBe(false);
  });
}
