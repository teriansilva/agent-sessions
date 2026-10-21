/** The dashboard paints its last read at once and revalidates behind a thin bar (#1223).
 *
 *  Real browser, desktop + mobile, API mocked. Every read of the dashboard sits behind a GATE the
 *  spec opens by hand, so the return visit can be observed while its reads are still in flight and
 *  then released one source at a time — which is what proves the bar belongs to the LAST applicable
 *  refresh, not the first. RECENT WORK is mounted only while its disclosure is open: its retained
 *  snapshot revalidates on each opening, and closing retires pending snapshot and AI reads.
 *  Navigation is in-app (pushState + popstate), never a reload: retention lives above the router
 *  for the page's lifetime and is deliberately not persisted. */
import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = Math.floor(Date.now() / 1000);
const A = "claude:aaaaaaaa-0000-4000-8000-00000000000a";

type Source = "sessions" | "missions" | "quota" | "needs" | "recap";
const SOURCES: Source[] = ["sessions", "missions", "quota", "needs", "recap"];
const EAGER_SOURCES = SOURCES.filter((src) => src !== "recap");

function recap(text = "Shipped the retained dashboard", stale = false) {
  return {
    window_days: 1,
    source: "local",
    generated_at: NOW,
    stale,
    configured: true,
    error: null,
    entries: [
      {
        session_key: A,
        ts: NOW - 600,
        text,
        engine: "claude",
        title: "retention",
        project: { id: "p1", name: "Alpha" },
        session_recap: "",
      },
    ],
  };
}

const BODIES: Record<Source, (url: URL) => unknown> = {
  sessions: () => ({
    live: {
      health: "ok",
      total: 7,
      working: 3,
      by_engine: { claude: { live: 7, working: 3 } },
      rows: [
        {
          id: A,
          title: "retained live session",
          engine: "claude",
          project: { id: "p1", name: "Alpha" },
          working: true,
          last_activity: NOW - 60,
          state_line: "running",
        },
      ],
    },
    recent: { total: 0, rows: [] },
  }),
  missions: (url) => {
    const state = url.searchParams.get("state");
    const rows =
      state === "running"
        ? [
            {
              id: "m1",
              title: "retained mission",
              project_id: null,
              cwd: null,
              state: "running",
              created_at: NOW - 7200,
              updated_at: NOW - 120,
              closed_at: null,
              archived_at: null,
              outcome: null,
              session_keys: [A],
            },
          ]
        : [];
    return {
      missions: rows,
      total: rows.length,
      limit: 5,
      offset: 0,
      facets: { projects: [], states: [] },
    };
  },
  quota: () => ({
    budgets: {},
    agents: [
      {
        engine: "claude",
        source: "plan",
        windows: [{ label: "weekly", used_pct: 82, resets_at: NOW + 86400 }],
        at: NOW - 400,
        checked_at: NOW - 400,
        stale: false,
        limit_tokens: 0,
        manual_used: 0,
        used_pct: 82,
      },
    ],
  }),
  needs: () => ({
    rows: [],
    total: 0,
    total_unfiltered: 2,
    needs_you_ids: [A, "codex:bbbbbbbb-0000-4000-8000-00000000000b"],
    truncated: false,
    facets: { engines: [], projects: [] },
    window_days: 1,
  }),
  recap: () => recap(),
};

/** One gate per source. While `held`, every request to it waits until `release` — which answers
 *  all of them, with the real body or (for `fail`) a 500. */
class Gates {
  held = new Set<Source>();
  never = new Set<Source>();
  bodies: Partial<Record<Source, (url: URL) => unknown>> = {};
  private waiting = new Map<Source, Array<(fail: boolean) => void>>();
  count = new Map<Source, number>();

  async answer(src: Source, route: Route) {
    this.count.set(src, (this.count.get(src) ?? 0) + 1);
    if (this.never.has(src)) return; // never answered: the source stays cold
    // Capture each request's answer before its gate: late reads retain their old snapshot even
    // when the next opening has already asked for newer data.
    const url = new URL(route.request().url());
    const body = (this.bodies[src] ?? BODIES[src])(url);
    let fail = false;
    if (this.held.has(src)) {
      fail = await new Promise<boolean>((res) => {
        this.waiting.set(src, [...(this.waiting.get(src) ?? []), res]);
      });
    }
    return fail
      ? route.fulfill({ status: 500, json: { detail: "down" } })
      : route.fulfill({ json: body });
  }

  holdAll() {
    for (const s of SOURCES) this.held.add(s);
  }

  release(src: Source, fail = false) {
    this.held.delete(src);
    for (const w of this.waiting.get(src) ?? []) w(fail);
    this.waiting.delete(src);
  }

  pending(src: Source) {
    return (this.waiting.get(src) ?? []).length;
  }

  releaseOne(src: Source, newest = false) {
    const waiting = this.waiting.get(src) ?? [];
    const resolve = newest ? waiting.pop() : waiting.shift();
    resolve?.(false);
    if (waiting.length === 0) this.waiting.delete(src);
  }
}

async function mock(page: Page, gates: Gates) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true, window_days: 1 },
        new_session_engines: ["claude"],
        onboarded: true,
        project_roots: [],
        folder_exclusions: [],
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/dashboard\/sessions/, (r) =>
    gates.answer("sessions", r),
  );
  await page.route("**/api/missions**", (r) => gates.answer("missions", r));
  await page.route("**/api/agents/usage", (r) => gates.answer("quota", r));
  await page.route(/\/api\/pulse\/needs-you(\?.*)?$/, (r) =>
    gates.answer("needs", r),
  );
  await page.route(/\/api\/pulse\/recap(\?.*)?$/, (r) =>
    gates.answer("recap", r),
  );
}

async function navigateInApp(page: Page, path: string) {
  await page.evaluate((p) => {
    window.history.pushState({}, "", p);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

const bar = (page: Page) => page.getByTestId("dashboard-refresh-bar");
const toggle = (page: Page) => page.getByTestId("dashboard-details-toggle");

async function openRecentWork(page: Page) {
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await toggle(page).click();
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
}

async function closeRecentWork(page: Page) {
  await toggle(page).click();
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#dashboard-details-content")).toBeHidden();
  await expect(page.getByTestId("recent-work")).toHaveCount(0);
}

async function firstVisitSettled(page: Page) {
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(page.getByTestId("kpi-needs")).toContainText("2");
  await openRecentWork(page);
  await expect(page.getByText("Shipped the retained dashboard")).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
}

/** Leave for another section and come back with every read held. */
async function leaveAndReturnHeld(page: Page, gates: Gates) {
  await navigateInApp(page, "/settings");
  await expect(page.getByTestId("dashboard-page")).toHaveCount(0);
  gates.holdAll();
  await navigateInApp(page, "/dashboard");
  await expect(page.getByTestId("dashboard-page")).toBeVisible();
}

test("coming back paints the last dashboard at once, and the bar stays until the LAST re-read settles", async ({
  page,
}) => {
  const gates = new Gates();
  await mock(page, gates);
  await page.goto("/dashboard");
  // The first visit is cold and draws no bar: there is nothing painted to refresh.
  await firstVisitSettled(page);

  await leaveAndReturnHeld(page, gates);
  await openRecentWork(page);
  // Painted at once — no skeleton — while every source re-reads behind it.
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(page.getByTestId("kpi-needs")).toContainText("2");
  await expect(page.getByTestId("kpi-missions")).toContainText("1 active");
  await expect(page.getByTestId("kpi-quota")).toContainText("18%");
  await expect(page.getByText("Shipped the retained dashboard")).toBeVisible();
  await expect(page.getByTestId("tile-loading")).toHaveCount(0);
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  await expect(
    page.getByRole("progressbar", { name: "Refreshing dashboard" }),
  ).toBeVisible();
  // Every entry revalidates: each source was asked again, not served from memory.
  for (const s of SOURCES)
    await expect.poll(() => gates.pending(s)).toBeGreaterThan(0);

  // On a phone every KPI and tile stays reachable: nothing pushes the page sideways.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);

  // Released one source at a time — one of them refused. The bar outlives every one but the last.
  gates.release("sessions");
  gates.release("quota", true);
  gates.release("recap");
  gates.release("missions");
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  // The refused re-read kept its data, and says so.
  await expect(page.getByTestId("kpi-quota")).toContainText("18%");
  await expect(
    page.getByTestId("dash-quota").getByTestId("tile-refresh-error"),
  ).toBeVisible();

  gates.release("needs");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
  await expect(page.getByRole("progressbar")).toHaveCount(0);
});

test("a source never read before stays cold while the others paint (mixed warm/cold)", async ({
  page,
}) => {
  const gates = new Gates();
  gates.never.add("missions"); // the first visit never hears back from the mission store
  await mock(page, gates);
  await page.goto("/dashboard");
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(
    page.getByTestId("dash-missions").getByTestId("tile-loading"),
  ).toBeVisible();

  gates.never.delete("missions");
  await leaveAndReturnHeld(page, gates);
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(
    page.getByTestId("dash-missions").getByTestId("tile-loading"),
  ).toBeVisible();
  await expect(bar(page)).toHaveAttribute("data-active", "true");

  for (const s of SOURCES) gates.release(s);
  await expect(page.getByTestId("kpi-missions")).toContainText("1 active");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
});

test("a Retry over a failed warm NEEDS YOU read shows the bar until it settles (review 5407)", async ({
  page,
}) => {
  const gates = new Gates();
  await mock(page, gates);
  await page.goto("/dashboard");
  await firstVisitSettled(page);

  await leaveAndReturnHeld(page, gates);
  // Only once the return visit's read is AT the gate — releasing earlier answers nobody, and the
  // read that arrives after is then answered normally.
  await expect.poll(() => gates.pending("needs")).toBeGreaterThan(0);
  gates.release("needs", true); // the warm re-read fails: the list stays, marked
  for (const s of SOURCES) if (s !== "needs") gates.release(s);
  await expect(page.getByTestId("needs-you-refresh-error")).toBeVisible();
  await expect(bar(page)).toHaveAttribute("data-active", "false");

  gates.held.add("needs");
  await page
    .getByTestId("needs-you-refresh-error")
    .getByRole("button", { name: "Retry" })
    .click();
  // The list is still painted, so re-reading it is a refresh — the bar says so.
  await expect.poll(() => gates.pending("needs")).toBeGreaterThan(0);
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  await expect(page.getByTestId("kpi-needs")).toContainText("2");

  gates.release("needs");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
  await expect(page.getByTestId("needs-you-refresh-error")).toHaveCount(0);
});

test("Recent work stays closed on return; eager reads settle the bar before its retained recap revalidates on opening", async ({
  page,
}) => {
  const gates = new Gates();
  await mock(page, gates);
  await page.goto("/dashboard");
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(page.getByTestId("kpi-missions")).toContainText("1 active");
  await expect(page.getByTestId("kpi-quota")).toContainText("18%");
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  expect(gates.count.get("recap") ?? 0).toBe(0);
  await firstVisitSettled(page);

  await leaveAndReturnHeld(page, gates);
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByTestId("recent-work")).toHaveCount(0);
  expect(gates.count.get("recap")).toBe(1);
  expect(gates.pending("recap")).toBe(0);
  for (const src of EAGER_SOURCES)
    await expect.poll(() => gates.pending(src)).toBeGreaterThan(0);
  await expect(bar(page)).toHaveAttribute("data-active", "true");

  for (const src of EAGER_SOURCES) gates.release(src);
  await expect(bar(page)).toHaveAttribute("data-active", "false");
  // Hidden recap is still held but was never requested, so it cannot keep the page refreshing.
  expect(gates.pending("recap")).toBe(0);

  await openRecentWork(page);
  await expect(page.getByText("Shipped the retained dashboard")).toBeVisible();
  await expect.poll(() => gates.pending("recap")).toBe(1);
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  gates.release("recap");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
});

test("closing a warm recap reread clears the bar, and its late snapshot cannot replace a newer opening or its retained data", async ({
  page,
}) => {
  const gates = new Gates();
  await mock(page, gates);
  await page.goto("/dashboard");
  await firstVisitSettled(page);
  await closeRecentWork(page);

  gates.bodies.recap = () => recap("Retired recap snapshot");
  gates.held.add("recap");
  await openRecentWork(page);
  await expect.poll(() => gates.pending("recap")).toBe(1);
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  await closeRecentWork(page);
  await expect(bar(page)).toHaveAttribute("data-active", "false");

  gates.bodies.recap = () => recap("Newest recap snapshot");
  await openRecentWork(page);
  await expect.poll(() => gates.pending("recap")).toBe(2);
  gates.releaseOne("recap", true); // The new opening answers before the retired one.
  await expect(page.getByText("Newest recap snapshot")).toBeVisible();
  await expect(bar(page)).toHaveAttribute("data-active", "false");

  const retiredResponse = page.waitForResponse(/\/api\/pulse\/recap(\?.*)?$/);
  gates.releaseOne("recap");
  await (await retiredResponse).finished();
  await expect(page.getByText("Retired recap snapshot")).toHaveCount(0);
  await expect(page.getByText("Newest recap snapshot")).toBeVisible();

  await closeRecentWork(page);
  await openRecentWork(page);
  await expect.poll(() => gates.pending("recap")).toBe(1);
  // The next opening paints the newest retained snapshot while its new request is still held.
  await expect(page.getByText("Newest recap snapshot")).toBeVisible();
  await expect(page.getByText("Retired recap snapshot")).toHaveCount(0);
  await expect(bar(page)).toHaveAttribute("data-active", "true");
  gates.release("recap");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
});

test("closing during an AI recap refresh retires its answer without holding the bar or overwriting a later opening", async ({
  page,
}) => {
  const gates = new Gates();
  await mock(page, gates);
  let releaseAi = () => {};
  const aiGate = new Promise<void>((resolve) => {
    releaseAi = resolve;
  });
  let aiRequests = 0;
  await page.route(/\/api\/pulse\/recap(\?.*)?$/, async (route) => {
    if (route.request().method() !== "POST")
      return gates.answer("recap", route);
    aiRequests++;
    await aiGate;
    return route.fulfill({ json: recap("Retired AI recap") });
  });
  await page.goto("/dashboard");
  await firstVisitSettled(page);
  await closeRecentWork(page);

  gates.bodies.recap = () => recap("Snapshot awaiting AI", true);
  await openRecentWork(page);
  await expect.poll(() => aiRequests).toBe(1);
  await expect(page.getByTestId("recent-work")).toContainText("updating…");
  // The follow-on AI call has its own indicator, never the dashboard snapshot refresh bar.
  await expect(bar(page)).toHaveAttribute("data-active", "false");
  await closeRecentWork(page);
  await expect(bar(page)).toHaveAttribute("data-active", "false");

  gates.bodies.recap = () => recap("Snapshot after reopening");
  await openRecentWork(page);
  await expect(page.getByText("Snapshot after reopening")).toBeVisible();
  const retiredResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/pulse\/recap(\?.*)?$/.test(response.url()),
  );
  releaseAi();
  await (await retiredResponse).finished();
  await expect(page.getByText("Retired AI recap")).toHaveCount(0);
  await expect(page.getByText("Snapshot after reopening")).toBeVisible();
  await expect(bar(page)).toHaveAttribute("data-active", "false");
  expect(aiRequests).toBe(1);

  await closeRecentWork(page);
  gates.held.add("recap");
  await openRecentWork(page);
  await expect.poll(() => gates.pending("recap")).toBe(1);
  await expect(page.getByText("Snapshot after reopening")).toBeVisible();
  await expect(page.getByText("Retired AI recap")).toHaveCount(0);
  gates.release("recap");
  await expect(bar(page)).toHaveAttribute("data-active", "false");
});
