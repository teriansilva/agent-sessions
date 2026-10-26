import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { agentPicker } from "./newSessionForm";

// #1647, real browser, desktop + mobile, dark + light. Two things the operator hit on the map:
//  - a skip start on a loaded host took ~23 s, and a read gap inside it flashed LINK LOST, then
//    RESTORED, over a session that was simply starting. A gap shorter than 15 s now stays inside
//    the Starting state;
//  - a discarded creation kept a live composer whose every send was refused. It now says so and
//    its composer is off.
// (Keeping the API composer's draft across an unmount moved to its own issue; #1648 keeps the map
// mounted through New session.)
// Set CAPTURE_DIR to also write the state board PNGs attached to #1647.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const ENGINE = "claude-api";
const KEY = `${ENGINE}:${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);

type Json = Record<string, unknown>;

function snapshot(over: Json = {}): Json {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/home/u/battlelab",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
    bypass: true,
    ...over,
  };
}

/** The app around one session; `view` answers each snapshot read (null = the read fails). */
async function attach(page: Page, view: () => Json | null, theme: string) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "test",
        terminal_backend: "ws",
        auth_mode: "none",
        theme,
        overview_expanded: [],
        projects_hidden: [],
        new_session_engines: [ENGINE],
        default_project: "/home/u/battlelab",
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page);
  await page.route(SESSION, async (r: Route) => {
    const sub = r.request().url().match(SESSION)?.[1] ?? "";
    if (sub === "/events") {
      // The cursor read fails whenever the snapshot does: one link, one outage.
      if (view() === null) return r.fulfill({ status: 503, json: { detail: "busy" } });
      return r.fulfill({ json: { session_key: KEY, revision: 4, next_cursor: 4, events: [] } });
    }
    if (sub) return r.fulfill({ status: 404, json: { detail: "not mocked" } });
    const v = view();
    return v ? r.fulfill({ json: v }) : r.fulfill({ status: 503, json: { detail: "busy" } });
  });
}

const box = (page: Page) => page.getByRole("textbox", { name: /^Message / });

async function board(page: Page, name: string, project: string, theme: string) {
  const dir = process.env.CAPTURE_DIR;
  if (dir) await page.screenshot({ path: `${dir}/1647-${name}-${project}-${theme}.png` });
}

for (const theme of ["dark", "light"]) {
  test(`a read gap during a slow start stays inside Starting — no veil, no lost-connection notice (${theme})`, async ({ page }, info) => {
    const t0 = { at: 0 };
    // Starting for 1.5 s, then 8 s of failed reads (past the veil's ordinary 5 s grace), then ready.
    await attach(
      page,
      () => {
        const t = Date.now() - t0.at;
        if (t < 1_500) {
          return snapshot({ start_in_flight: true, launch: { gen: "w1", state: "starting", since: 1 } });
        }
        if (t < 9_500) return null;
        return snapshot({ launch: { gen: "w1", state: "ready", since: 2 } });
      },
      theme,
    );
    t0.at = Date.now();
    await page.goto(`/s/${ENGINE}/${ID}`);
    await expect(page.getByTestId("structured-starting")).toBeVisible();
    expect(await page.getByText("This session’s start didn’t finish").count()).toBe(0);
    await board(page, "starting", info.project.name, theme);
    const send = page.getByTestId("structured-send");
    // Instant counts, sampled every 250 ms: a retrying `toHaveCount(0)` would wait out a veil that
    // comes and goes, and pass on the client before #1647.
    const shown = async () =>
      (await page.getByTestId("uplink-veil").count()) +
      (await page.getByTestId("structured-reconnecting").count());
    while (!/^\s*Send\s*$/.test((await send.textContent()) ?? "")) {
      expect(Date.now() - t0.at, "the start never settled").toBeLessThan(20_000);
      expect(await shown(), `veil or lost-connection notice at ${Date.now() - t0.at} ms`).toBe(0);
      await page.waitForTimeout(250);
    }
    for (let i = 0; i < 8; i += 1) {
      // Nothing after ready either: no RESTORED for an outage that was never shown.
      expect(await shown()).toBe(0);
      expect(await page.getByText(/restored/i).count()).toBe(0);
      await page.waitForTimeout(250);
    }
    await expect(page.getByTestId("structured-starting")).toHaveCount(0);
  });

  test(`a discarded creation says so and its composer is off (${theme})`, async ({ page }, info) => {
    await attach(
      page,
      () => snapshot({ start_discarded: true, native: { native_id: null, worker: null, background_active: false } }),
      theme,
    );
    await page.goto(`/s/${ENGINE}/${ID}`);
    await expect(page.getByTestId("structured-start-discarded")).toHaveText(
      "This session was discarded before it started. Nothing ran.",
    );
    await expect(box(page)).toBeDisabled();
    await expect(box(page)).toHaveAttribute("placeholder", "This session was discarded");
    await expect(page.getByTestId("structured-send")).toBeDisabled();
    // Every other writer of the draft is off with it (Hermes on #1647: the stills could not show it).
    await expect(page.getByRole("button", { name: "Use a template" })).toBeDisabled();
    const talk = page.getByRole("button", { name: /voice input/i });
    if (await talk.count()) await expect(talk).toBeDisabled();
    await board(page, "discarded", info.project.name, theme);
  });
}

// The veil's boundaries around the start hold (Hermes on #1647, round 5): a real outage still
// surfaces, a pane that has read nothing is never shown connected, and once the start has settled
// the ordinary 5 s grace is back.
const veilCount = (page: Page) => page.getByTestId("uplink-veil").count();

test("a prolonged outage during a start still shows the veil, after 15 s", async ({ page }) => {
  const t0 = { at: 0 };
  await attach(
    page,
    () =>
      Date.now() - t0.at < 1_500
        ? snapshot({ start_in_flight: true, launch: { gen: "w1", state: "starting", since: 1 } })
        : null, // the link never comes back
    "dark",
  );
  t0.at = Date.now();
  await page.goto(`/s/${ENGINE}/${ID}`);
  await expect(page.getByTestId("structured-starting")).toBeVisible();
  await page.waitForTimeout(10_000 - (Date.now() - t0.at)); // 8.5 s into the gap: still held
  expect(await veilCount(page)).toBe(0);
  await expect(page.getByTestId("uplink-veil")).toBeVisible({ timeout: 12_000 });
  await expect(page.getByTestId("uplink-veil")).toHaveAttribute("data-phase", "lost");
  expect(Date.now() - t0.at).toBeGreaterThan(15_500); // never before the hold and the grace
  await expect(page.getByTestId("structured-reconnecting")).toBeVisible();
});

test("once the start has settled, a read gap gets the ordinary 5 s grace again", async ({ page }) => {
  const t0 = { at: 0, failed: 0 };
  await attach(
    page,
    () => {
      const t = Date.now() - t0.at;
      if (t < 1_500) return snapshot({ start_in_flight: true, launch: { gen: "w1", state: "starting", since: 1 } });
      if (t < 3_000) return snapshot({ launch: { gen: "w1", state: "ready", since: 2 } });
      if (!t0.failed) t0.failed = Date.now(); // the first read the pane makes after 3 s fails
      return null;
    },
    "dark",
  );
  t0.at = Date.now();
  await page.goto(`/s/${ENGINE}/${ID}`);
  await expect(page.getByTestId("structured-send")).toHaveText(/^\s*Send\s*$/, { timeout: 5_000 });
  await expect(page.getByTestId("uplink-veil")).toBeVisible({ timeout: 25_000 });
  const gap = Date.now() - t0.failed;
  // The ordinary 5 s grace from the first failed read (plus a backoff tick), never the start hold.
  expect(gap).toBeGreaterThan(4_500);
  expect(gap).toBeLessThan(9_000);
});

test("a start whose first read fails gets no hold: the pane is never shown connected", async ({ page }) => {
  const id = "6c1e3d2f-9a4b-4d8e-af32-7b5c4d3e2f10";
  const key = `codex-api:${id}`;
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
        agent_defaults: { bypass: true },
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
  await page.route("**/api/structured/sessions", (r) =>
    r.fulfill({ status: 201, json: snapshot({ session_key: key, pending_start: true }) }),
  );
  const session = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${id}(/[a-z]+)?(\\?.*)?$`);
  await page.route(session, async (r) => {
    const sub = r.request().url().match(session)?.[1] ?? "";
    if (sub === "/start") return; // the handed start never answers: it stays in flight
    return r.fulfill({ status: 503, json: { detail: "busy" } }); // nothing is ever readable
  });
  await page.goto(NEW_SESSION_PATH);
  await agentPicker(page).selectOption("codex-api");
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${id}$`));
  // The ordinary first-connect veil; never the Starting state or a live worker over an unread pane.
  await expect(page.getByTestId("uplink-veil")).toHaveAttribute("data-phase", "establishing", { timeout: 8_000 });
  expect(await page.getByTestId("structured-starting").count()).toBe(0);
  expect(await page.getByText("worker live").count()).toBe(0);
});
