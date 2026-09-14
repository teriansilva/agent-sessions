import { expect, type Page, test } from "@playwright/test";
import { promptPath, settingsPath } from "../src/routes/settingsTabs";

// Real-browser checks for the AI session review surface (#356 PR 1, manual slice):
// Settings → AI → Endpoint & model (the two-step connection/model flow, the write-only key, the
// model list through the server proxy, and every stale-response case #956 pins) and Session
// review (link into the prompt catalog) and the sidebar row (summary line + amber intervention badge). Network is
// fully mocked — the suite never talks to a backend or a real AI endpoint.

const AI_REVIEW = {
  enabled: false,
  base_url: "https://ai.example.io/v1",
  model: "minimax-m2.7",
  interval_minutes: 5,
  max_input_chars: 24000,
  api_key_set: true,
  configured: true,
};

const AUTO_SORT = {
  enabled: false,
  interval_minutes: 30,
  confidence_min: 0.7,
  max_per_pass: 8,
  configured: true,
};

const NOW = Math.floor(Date.now() / 1000);

const SESSIONS = {
  sessions: [
    {
      id: "claude:aaaaaaaa-0000-0000-0000-000000000001",
      engine: "claude",
      uuid: "aaaaaaaa-0000-0000-0000-000000000001",
      short_uuid: "aaaaaaaa",
      cwd: "/home/u/infra",
      project: { kind: "folder", id: "/home/u/infra", name: "/home/u/infra" },
      last_mtime: NOW,
      first_user_message: "fix the runner",
      title: "Fix CI runner fork-EAGAIN limits",
      sticky: false,
      archived: false,
      ai_summary: "Editing systemd limits; tests rerunning after thread cap",
      ai_title: "Fix CI runner fork-EAGAIN limits",
      intervention_required: true,
      intervention_reason: "waiting on permission prompt",
      reviewed_at: NOW,
      review_excluded: false,
    },
  ],
  next_offset: null,
  total: 1,
  facets: {
    projects: [{ kind: "folder", id: "/home/u/infra", name: "/home/u/infra" }],
    engines: ["claude"],
  },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: AI_REVIEW,
        auto_sort: AUTO_SORT,
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  // The auto-sort section resolves near-miss project names via /api/projects on mount.
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: SESSIONS }));
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({
      json: { models: ["minimax-m2.7", "qwen3-vl", "gpt-oss-120b"] },
    }),
  );
  // The draft check (#956) answers with the same list unless a test says otherwise.
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({
      json: {
        models: ["minimax-m2.7", "qwen3-vl", "gpt-oss-120b"],
        listing: "ok",
      },
    }),
  );
});

/** A server whose /api/config reflects what /api/prefs stored — the real contract, so the config
 *  refresh after a save shows that save. `hold` delays every prefs response until it resolves
 *  (the body is recorded first), which is how an accepted-but-slow save is staged. */
async function statefulServer(page: Page, hold?: Promise<void>) {
  const ai: Record<string, unknown> = { request_timeout: null, ...AI_REVIEW };
  const posts: Record<string, unknown>[] = [];
  let configCalls = 0;
  await page.route("**/api/config", (r) => {
    configCalls += 1;
    return r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: { ...ai },
        auto_sort: AUTO_SORT,
      },
    });
  });
  await page.route("**/api/prefs", async (r) => {
    const body = (r.request().postDataJSON() ?? {}) as {
      ai_review?: Record<string, unknown>;
    };
    posts.push(body);
    if (hold) await hold;
    const patch = body.ai_review ?? {};
    for (const k of ["base_url", "model", "request_timeout"]) {
      if (k in patch) ai[k] = patch[k];
    }
    if ("api_key" in patch) ai.api_key_set = patch.api_key !== null;
    ai.configured = ai.base_url !== "" && ai.api_key_set === true;
    await r.fulfill({ json: { ai_review: { ...ai } } });
  });
  return { posts, configCalls: () => configCalls };
}

/** Hold a route until the test releases it — a slow gateway. */
function held() {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  return { gate, release };
}

/** In-app section switch (no reload): the sidebar on desktop, back to the index on a phone. */
async function openSection(page: Page, label: string) {
  const back = page.getByRole("link", { name: "Back to settings" });
  if (await back.isVisible()) {
    await back.click();
  }
  const nav = page.getByRole("navigation", { name: "Settings", exact: true });
  await nav
    .getByRole("link")
    .filter({ has: page.getByText(label, { exact: true }) })
    .click();
}

/** Give the page time to act on a response it is expected to IGNORE. There is nothing to wait
 *  for when the correct behaviour is "nothing happens", so the proof is a bounded settle. */
const settle = (page: Page) =>
  page.evaluate(() => new Promise((r) => setTimeout(r, 400)));

test("settings: picking a model does not save it — Save model stores model and timeout together", async ({
  page,
}) => {
  const server = await statefulServer(page);
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("heading", { name: "Connection" })).toBeVisible();
  // The endpoint renders from /api/config; with a key stored there is a readout, not a field.
  await expect(page.getByLabel(/Base URL/i)).toHaveValue(
    "https://ai.example.io/v1",
  );
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.getByText("set", { exact: true })).toBeVisible();

  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toHaveValue("minimax-m2.7");
  await model.selectOption("qwen3-vl");
  await expect(
    page.getByText("● Unsaved — active is still minimax-m2.7."),
  ).toBeVisible();
  expect(server.posts).toEqual([]);

  await page.getByLabel("Request timeout").fill("240");
  await page.getByRole("button", { name: "Save model" }).click();
  await expect
    .poll(() => server.posts)
    .toEqual([{ ai_review: { model: "qwen3-vl", request_timeout: 240 } }]);
  await expect(
    page.getByText(/✓ Model saved — active: qwen3-vl · 240 s timeout/),
  ).toBeVisible();

  // The prompt is not edited here (#824) — Session review links into the catalog row.
  await page.goto(settingsPath("ai-session-review"));
  const review = page.getByRole("region", { name: "Session review" });
  await expect(review.getByRole("textbox", { name: "Review prompt" })).toHaveCount(0);
  await expect(
    review.getByRole("link", { name: /Prompts → Tail review/i }),
  ).toHaveAttribute("href", promptPath("tail_review"));
});

test("settings: a plain visit is quiet — the saved connection listed, a readout, nothing unsaved (#543)", async ({
  page,
}) => {
  const server = await statefulServer(page);
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  await expect(page.getByTestId("endpoint-status")).toContainText("Connected");
  await expect(page.getByText(/● Unsaved/)).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save connection" }),
  ).toBeDisabled();
  expect(server.posts).toEqual([]);
  // The key field exists only after Replace key (#834), and opts out of password managers.
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await page.getByRole("button", { name: "Replace key" }).click();
  await expect(page.getByLabel(/API key/i)).toHaveAttribute(
    "autocomplete",
    "new-password",
  );
});

test("settings: a failed check saves nothing and says why; Save without testing stores it on purpose (#834/#956)", async ({
  page,
}) => {
  const gateway =
    "LiteLLM Virtual Key expected. Received=abc…, expected to start with 'sk-'";
  const server = await statefulServer(page);
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({ status: 502, json: { detail: gateway } }),
  );
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  await page.getByRole("button", { name: "Replace key" }).click();
  await page.getByLabel(/API key/i).fill("sk-wrong-key");
  await page.getByRole("button", { name: "Save connection" }).click();

  await expect(page.getByText(`✗ Not saved — ${gateway}`)).toBeVisible();
  expect(server.posts).toEqual([]); // the saved config is untouched
  await expect(page.getByLabel(/API key/i)).toHaveValue("sk-wrong-key"); // nothing to retype

  await page.getByRole("button", { name: "Save without testing" }).click();
  await expect
    .poll(() => server.posts)
    .toEqual([
      {
        ai_review: {
          base_url: "https://ai.example.io/v1",
          api_key: "sk-wrong-key",
        },
      },
    ]);
  await expect(page.getByText("Saved without testing.")).toBeVisible();
});

test("settings: a failed check of the SAVED connection shows the gateway's reason and Check failed (#834)", async ({
  page,
}) => {
  const gateway = "LiteLLM Virtual Key expected";
  await statefulServer(page);
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ status: 502, json: { detail: gateway } }),
  );
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByText(`✗ ${gateway}`)).toBeVisible();
  await expect(page.getByTestId("endpoint-status")).toContainText(
    "Check failed",
  );
  await expect(page.getByText(/GET \/api\/ai-review\/models/)).toHaveCount(0);
});

test("settings: a stored key has no fillable field until Replace key; a new one saves once and folds away (#834)", async ({
  page,
}) => {
  const server = await statefulServer(page);
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  await page.getByRole("button", { name: "Replace key" }).click();
  await expect(page.getByLabel(/API key/i)).toHaveValue("");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  expect(server.posts).toEqual([]); // cancelling never touches the stored key

  await page.getByRole("button", { name: "Replace key" }).click();
  await page.getByLabel(/API key/i).fill("sk-rotated");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect
    .poll(() => server.posts)
    .toEqual([
      {
        ai_review: { base_url: "https://ai.example.io/v1", api_key: "sk-rotated" },
      },
    ]);
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.getByText(/✓ Connected — 3 models available/)).toBeVisible();
});

test("settings: while the connection has unsaved edits the model step LOOKS locked and says why (#834)", async ({
  page,
}) => {
  await statefulServer(page);
  await page.goto(settingsPath("ai-endpoint"));
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toBeEnabled();
  await page.getByLabel(/Base URL/i).fill("https://ai.example.io/v2");
  await expect(model).toBeDisabled();
  await expect(
    page.getByText(/Save the connection first — this list belongs to the saved endpoint/),
  ).toBeVisible();
  const opacity = await model.evaluate((el) =>
    Number(getComputedStyle(el).opacity),
  );
  expect(opacity).toBeLessThan(1);
});

test("settings: an endpoint that can't list models is saved, and the model step takes a typed id (#956)", async ({
  page,
}) => {
  const server = await statefulServer(page);
  await page.route("**/api/ai-review/endpoint/test", (r) =>
    r.fulfill({ json: { models: [], listing: "unsupported" } }),
  );
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: [] } }),
  );
  await page.goto(settingsPath("ai-endpoint"));
  await page.getByLabel(/Base URL/i).fill("https://ai.example.io/openai");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(
    page.getByText(/Saved — this endpoint doesn’t list models; type the model id below/),
  ).toBeVisible();
  expect(server.posts).toEqual([
    { ai_review: { base_url: "https://ai.example.io/openai" } },
  ]);
  const model = page.getByRole("textbox", { name: "Model" });
  await expect(model).toBeEnabled();
  await model.fill("gpt-oss-120b");
  await page.getByRole("button", { name: "Save model" }).click();
  await expect
    .poll(() => server.posts.at(-1))
    .toEqual({ ai_review: { model: "gpt-oss-120b", request_timeout: null } });
});

test("settings: editing while Save connection's check is pending saves nothing and publishes nothing (#956)", async ({
  page,
}) => {
  const server = await statefulServer(page);
  const slow = held();
  await page.route("**/api/ai-review/endpoint/test", async (r) => {
    await slow.gate;
    await r.fulfill({ json: { models: ["late-model"], listing: "ok" } });
  });
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  const url = page.getByLabel(/Base URL/i);
  await url.fill("https://ai.example.io/v2");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(page.getByText(/then saving/)).toBeVisible();

  await url.fill("https://ai.example.io/v3"); // the operator keeps typing
  const answered = page.waitForResponse("**/api/ai-review/endpoint/test");
  slow.release();
  await answered;
  await settle(page);

  expect(server.posts).toEqual([]);
  await expect(page.getByText(/● Unsaved/)).toBeVisible();
  await expect(page.getByRole("option", { name: "late-model" })).toHaveCount(0);
  await expect(page.getByTestId("endpoint-status")).not.toContainText(
    "Check failed",
  );
});

test("settings: leaving the page while a check is pending saves nothing (#956)", async ({
  page,
}) => {
  const server = await statefulServer(page);
  const slow = held();
  await page.route("**/api/ai-review/endpoint/test", async (r) => {
    await slow.gate;
    await r.fulfill({ json: { models: ["late-model"], listing: "ok" } });
  });
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  await page.getByLabel(/Base URL/i).fill("https://ai.example.io/v2");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(page.getByText(/then saving/)).toBeVisible();

  await openSection(page, "Session review");
  await expect(page.getByRole("region", { name: "Session review" })).toBeVisible();
  const answered = page.waitForResponse("**/api/ai-review/endpoint/test");
  slow.release();
  await answered;
  await settle(page);
  expect(server.posts).toEqual([]);

  await openSection(page, "Endpoint & model");
  await expect(page.getByLabel(/Base URL/i)).toHaveValue(
    "https://ai.example.io/v1",
  );
  await expect(page.getByRole("option", { name: "late-model" })).toHaveCount(0);
});

test("settings: a save the server accepted is adopted after a newer edit, which stays unsaved (#956)", async ({
  page,
}) => {
  const slow = held();
  const server = await statefulServer(page, slow.gate);
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  const url = page.getByLabel(/Base URL/i);
  await url.fill("https://ai.example.io/v2");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect.poll(() => server.posts.length).toBe(1); // checked, and the save is in flight

  await url.fill("https://ai.example.io/v2-next");
  const saved = page.waitForResponse("**/api/prefs");
  slow.release();
  await saved;

  await expect(url).toHaveValue("https://ai.example.io/v2-next");
  await expect(page.getByText(/● Unsaved/)).toBeVisible();
  await expect.poll(() => server.configCalls()).toBeGreaterThan(1);
  // The accepted save IS the saved state now: typing it back leaves nothing unsaved.
  await url.fill("https://ai.example.io/v2");
  await expect(page.getByText(/● Unsaved/)).toHaveCount(0);
});

test("settings: a pick back to the previous model while Save model is in flight survives the accepted save (#956)", async ({
  page,
}) => {
  const slow = held();
  const server = await statefulServer(page, slow.gate);
  await page.goto(settingsPath("ai-endpoint"));
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toHaveValue("minimax-m2.7");
  await model.selectOption("qwen3-vl");
  await page.getByRole("button", { name: "Save model" }).click();
  await expect.poll(() => server.posts.length).toBe(1); // the save is in flight

  await model.selectOption("minimax-m2.7"); // back to what was saved before
  const saved = page.waitForResponse("**/api/prefs");
  slow.release();
  await saved;

  // The accepted save is the saved state; the newer pick is still the draft, unsaved and saveable.
  await expect(
    page.getByText("● Unsaved — active is still qwen3-vl."),
  ).toBeVisible();
  await expect(model).toHaveValue("minimax-m2.7");
  const save = page.getByRole("button", { name: "Save model" });
  await expect(save).toBeEnabled();
  await save.click();
  await expect
    .poll(() => server.posts)
    .toEqual([
      { ai_review: { model: "qwen3-vl", request_timeout: null } },
      { ai_review: { model: "minimax-m2.7", request_timeout: null } },
    ]);
});

test("settings: each save sits at the foot of the card it saves, never between two cards (#834/#956)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop geometry check");
  await statefulServer(page);
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  const connection = page.getByRole("region", { name: "Connection" });
  const modelCard = page.getByRole("region", { name: "Model" });
  const saveConn = connection.getByRole("button", { name: "Save connection" });
  const saveModel = modelCard.getByRole("button", { name: "Save model" });
  const [c, sc, m, sm] = await Promise.all([
    connection.boundingBox(),
    saveConn.boundingBox(),
    modelCard.boundingBox(),
    saveModel.boundingBox(),
  ]);
  // Inside its own card, in that card's foot.
  expect(sc!.y).toBeGreaterThan(c!.y + c!.height - 64);
  expect(sc!.y + sc!.height).toBeLessThanOrEqual(c!.y + c!.height);
  expect(sm!.y).toBeGreaterThan(m!.y + m!.height - 64);
  expect(sm!.y + sm!.height).toBeLessThanOrEqual(m!.y + m!.height);
  // A dirty form never grows the button (#834).
  const clean = sc!.height;
  await page.getByLabel(/Base URL/i).fill("https://ai.example.io/v2");
  await expect(page.getByText(/● Unsaved/)).toBeVisible();
  expect((await saveConn.boundingBox())!.height).toBe(clean);
});

test("settings: Remove key clears the stored secret and refetches /api/config", async ({
  page,
}) => {
  // Hermes #367: the blank field means "unchanged" — clearing needs the explicit Remove
  // key action (api_key: null), and a configured-flip must refetch the shared config so
  // sidebar gating updates without a reload.
  let prefsBody: unknown = null;
  let configCalls = 0;
  const cleared = { ...AI_REVIEW, api_key_set: false, configured: false };
  await page.route("**/api/config", (r) => {
    configCalls += 1;
    return r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        ai_review: configCalls > 1 ? cleared : AI_REVIEW,
      },
    });
  });
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: { ai_review: cleared } });
  });

  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByText("set", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Remove key" }).click();
  await expect.poll(() => prefsBody).toEqual({ ai_review: { api_key: null } });
  // The SET badge and the Remove action are gone; the shared config was refetched.
  await expect(page.getByText("set", { exact: true })).toBeHidden();
  await expect(page.getByRole("button", { name: "Remove key" })).toBeHidden();
  await expect.poll(() => configCalls).toBeGreaterThan(1);
});

test("sidebar: summary line + amber intervention badge with the reason as tooltip", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name === "mobile",
    "sidebar is off-canvas on mobile — desktop covers the row surface",
  );
  await page.goto("/");
  await expect(
    page.getByText("Editing systemd limits; tests rerunning after thread cap"),
  ).toBeVisible();
  const badge = page.getByRole("img", { name: /intervention required/i });
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute("title", "waiting on permission prompt");
});

test("mobile: the Endpoint & model page renders at phone width", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width layout check");
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ json: { ai_review: AI_REVIEW } }),
  );
  await page.goto(settingsPath("ai-endpoint"));
  await expect(page.getByRole("heading", { name: "Connection" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Model" })).toBeVisible();

  // The key row carries a field plus two actions (#834) — at phone width it must wrap
  // rather than squeeze the key readout to "*****…", and its buttons must clear the 44px
  // touch target (docs/design.md §8).
  // The key readout itself — the status strip also says "Key stored".
  const readout = page.getByText(/^\*+ stored$/);
  const replace = page.getByRole("button", { name: "Replace key" });
  const [ro, rb] = await Promise.all([
    readout.boundingBox(),
    replace.boundingBox(),
  ]);
  expect(ro!.width).toBeGreaterThan(200); // full-width line, not a crushed sliver
  expect(rb!.y).toBeGreaterThan(ro!.y + ro!.height - 1); // wrapped BELOW the field
  expect(rb!.height).toBeGreaterThanOrEqual(44);
  const remove = await page
    .getByRole("button", { name: "Remove key" })
    .boundingBox();
  expect(remove!.height).toBeGreaterThanOrEqual(44);
});
