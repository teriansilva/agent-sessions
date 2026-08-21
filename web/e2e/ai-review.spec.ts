import { expect, test } from "@playwright/test";

// Real-browser checks for the AI session review surface (#356 PR 1, manual slice):
// the Settings → AI Review panel (write-only key, model dropdown via the server proxy,
// link into the prompt catalog) and the sidebar row (summary line + amber intervention badge). Network is
// fully mocked — the suite never talks to a backend or a real AI endpoint.

const AI_REVIEW = {
  enabled: false,
  base_url: "https://ai.example.io/v1",
  model: "minimax-m2.7",
  interval_minutes: 5,
  prompt: "custom prompt",
  max_input_chars: 24000,
  api_key_set: true,
  configured: true,
  default_prompt: "default prompt from server",
};

const AUTO_SORT = {
  enabled: false,
  interval_minutes: 30,
  confidence_min: 0.7,
  max_per_pass: 8,
  prompt: "default sort prompt",
  configured: true,
  default_prompt: "default sort prompt",
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
});

test("settings: AI Review panel — write-only key, proxied model dropdown, prompt save", async ({
  page,
}) => {
  let prefsBody: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: { ai_review: AI_REVIEW } });
  });

  await page.goto("/settings/ai-review");
  await expect(
    page.getByRole("heading", { name: "AI endpoint" }),
  ).toBeVisible();

  // Endpoint config renders from /api/config; the key is write-only — with one stored the
  // panel shows a static readout (no fillable field) + the SET badge (#834).
  await expect(page.getByLabel(/Endpoint base URL/i)).toHaveValue(
    "https://ai.example.io/v1",
  );
  await expect(page.locator("#ai-api-key")).toHaveCount(0);
  await expect(page.getByText(/stored$/)).toBeVisible();
  await expect(page.getByText("set", { exact: true })).toBeVisible();

  // Model dropdown is populated through the server-side proxy (the key never left the server).
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toHaveValue("minimax-m2.7");
  await model.selectOption("qwen3-vl");
  await expect
    .poll(() => prefsBody)
    .toEqual({ ai_review: { model: "qwen3-vl" } });

  // The prompt itself is no longer edited here (#824) — this panel owns the endpoint, the
  // Prompts catalog owns every prompt. What stays is the link into the right row.
  const review = page.getByRole("region", { name: "Session review" });
  await expect(review.getByRole("textbox", { name: "Review prompt" })).toHaveCount(0);
  await expect(
    review.getByRole("link", { name: /Prompts → Tail review/i }),
  ).toHaveAttribute("href", "#prompt-tail_review");
});

test("settings: a plain visit with a stored config stays quiet — no phantom dirty/validating state (#543)", async ({
  page,
}) => {
  await page.goto("/settings/ai-review");
  // Mount probe done: the dropdown is populated through the proxy.
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  // The status line reports explicit actions only — a plain visit must show neither the
  // save-style validation lifecycle nor an unsaved-changes warning (#543).
  await expect(page.getByText(/Validating endpoint/i)).toBeHidden();
  await expect(page.getByText(/Endpoint validated/i)).toBeHidden();
  await expect(page.getByText(/● Unsaved changes/)).toBeHidden();
  // The key field opts out of password-manager autofill — browsers ignore "off" and would
  // fill a saved password here, dirtying the form. It only exists after "Replace key" now
  // (#834); the opt-out still rides it for the window in which it does exist.
  await page.getByRole("button", { name: "Replace key" }).click();
  await expect(page.getByLabel(/API key/i)).toHaveAttribute(
    "autocomplete",
    "new-password",
  );
});

test("settings: a rejected Save & validate says WHY — the dirty warning never swallows it (#834)", async ({
  page,
}) => {
  // The reported bug: a failed save left the panel showing a bare "● Unsaved changes" and
  // nothing else, because `endpointDirty` was tested before the error state — and a failed
  // save deliberately KEEPS the typed key, so the form stays dirty and the error branch was
  // unreachable. The user reads it as "it can't save a new key" with no reason given.
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ status: 422, json: { detail: "ai_review.base_url must be an http(s) URL" } }),
  );
  await page.goto("/settings/ai-review");
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );

  await page.getByRole("button", { name: "Replace key" }).click();
  await page.getByLabel(/API key/i).fill("sk-a-brand-new-key");
  await page.getByRole("button", { name: "Save & validate" }).click();

  // The server's own reason, verbatim — and NOT the dirty warning standing in for it.
  await expect(
    page.getByText(/ai_review\.base_url must be an http\(s\) URL/),
  ).toBeVisible();
  await expect(page.getByText(/● Unsaved changes/)).toBeHidden();
  // The typed key survives the failure — the user doesn't retype it to try again.
  await expect(page.getByLabel(/API key/i)).toHaveValue("sk-a-brand-new-key");

  // The verdict describes the values that were rejected, so the next edit retires it and
  // the dirty warning takes back over — a stale error must not stay pinned to new text.
  await page.getByLabel(/Endpoint base URL/i).fill("https://fixed.example.io/v1");
  await expect(
    page.getByText(/ai_review\.base_url must be an http\(s\) URL/),
  ).toBeHidden();
  await expect(page.getByText(/● Unsaved changes/)).toBeVisible();
});

test("settings: a rejected /models probe shows the GATEWAY's message, not a bare status (#834)", async ({
  page,
}) => {
  // The save succeeds, then the validation probe is rejected by the endpoint. `#382` says
  // that message renders verbatim — but the client's plain `getJson` reduced every non-2xx
  // to "GET /api/ai-review/models… → 502" and threw the server's `detail` away, so the
  // panel could only ever show the status code. Unit tests missed it: they mock the api
  // module, which is precisely the boundary that was dropping the text.
  const gateway =
    "model listing returned HTTP 401: Authentication Error - virtual key expected.";
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ json: { ai_review: AI_REVIEW } }),
  );
  await page.goto("/settings/ai-review");
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );
  // Only the save-time probe fails — the mount probe already populated the list.
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ status: 502, json: { detail: gateway } }),
  );

  await page.getByRole("button", { name: "Replace key" }).click();
  await page.getByLabel(/API key/i).fill("sk-wrong-key");
  await page.getByRole("button", { name: "Save & validate" }).click();

  await expect(page.getByText(`✗ ${gateway}`)).toBeVisible();
  await expect(page.getByText(/GET \/api\/ai-review\/models/)).toBeHidden();
});

test("settings: a stored key has no fillable field until Replace key (#834)", async ({
  page,
}) => {
  // Why this is structural and not another `autocomplete` hint: a persistent type=password
  // input is a password-manager magnet — the browser offers to SAVE whatever key is typed
  // there and refills it on every later visit, which left a plain visit permanently dirty
  // and put the refilled value one click from overwriting a working key. A field that isn't
  // on the page can't be filled.
  let prefsBody: unknown = null;
  await page.route("**/api/prefs", async (r) => {
    prefsBody = r.request().postDataJSON();
    await r.fulfill({ json: { ai_review: AI_REVIEW } });
  });
  await page.goto("/settings/ai-review");
  await expect(page.getByRole("combobox", { name: "Model" })).toHaveValue(
    "minimax-m2.7",
  );

  // A plain visit: readout only, and the form cannot be dirty because there is nothing to fill.
  await expect(page.locator("#ai-api-key")).toHaveCount(0);
  await expect(page.getByText(/● Unsaved changes/)).toBeHidden();
  await expect(
    page.getByRole("button", { name: "Save & validate" }),
  ).toBeDisabled();

  // Replace key reveals an EMPTY input; Cancel puts the secret back out of reach.
  await page.getByRole("button", { name: "Replace key" }).click();
  await expect(page.getByLabel(/API key/i)).toHaveValue("");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("#ai-api-key")).toHaveCount(0);
  expect(prefsBody).toBeNull(); // cancelling never touches the stored key

  // Replace → type → save: the new key goes up once and the field folds away again.
  await page.getByRole("button", { name: "Replace key" }).click();
  await page.getByLabel(/API key/i).fill("sk-rotated");
  await page.getByRole("button", { name: "Save & validate" }).click();
  await expect
    .poll(() => prefsBody)
    .toEqual({
      ai_review: { base_url: "https://ai.example.io/v1", api_key: "sk-rotated" },
    });
  await expect(page.locator("#ai-api-key")).toHaveCount(0);
  await expect(page.getByText(/● Unsaved changes/)).toBeHidden();
});

test("settings: the model control, when locked, LOOKS locked and says why (#834)", async ({
  page,
}) => {
  // `modelLocked` disables the select while the endpoint has uncommitted edits that no
  // validated probe backs (#394) — in practice, after a save fails. `.aiInput` had no
  // `:disabled` rule, so that dead control rendered identically to a live one: the panel
  // went unusable and the only way to find out was to click and get nothing.
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ status: 422, json: { detail: "nope" } }),
  );
  await page.goto("/settings/ai-review");
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toBeEnabled();

  await page.getByLabel(/Endpoint base URL/i).fill("https://other.example.io/v1");
  await page.getByRole("button", { name: "Save & validate" }).click();
  await expect(model).toBeDisabled();
  await expect(page.getByText(/Locked while the endpoint above/i)).toBeVisible();
  const opacity = await model.evaluate((el) =>
    Number(getComputedStyle(el).opacity),
  );
  expect(opacity).toBeLessThan(1);
});

test("settings: the endpoint's save row groups with the fields it commits (#834)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop spacing check");
  // The reported "the save button is over the model dropdown": the row sat 22px below the
  // key hint and 10px above MODEL, so it read as the dropdown's own save control.
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ json: { ai_review: AI_REVIEW } }),
  );
  await page.goto("/settings/ai-review");
  const save = page.getByRole("button", { name: "Save & validate" });
  await expect(save).toBeVisible();
  const hint = page.getByText(/Write-only: the stored key is never shown/);
  const modelLabel = page.getByText("Model", { exact: true });

  const [h, s, m] = await Promise.all([
    hint.boundingBox(),
    save.boundingBox(),
    modelLabel.boundingBox(),
  ]);
  const above = s!.y - (h!.y + h!.height); // gap to the group it belongs to
  const below = m!.y - (s!.y + s!.height); // gap to the field it does NOT commit
  expect(below).toBeGreaterThan(above);

  // …and the button must not resize when the status line appears beside it.
  const clean = (await save.boundingBox())!.height;
  await page.getByLabel(/Endpoint base URL/i).fill("https://other.example.io/v1");
  await expect(page.getByText(/● Unsaved changes/)).toBeVisible();
  expect((await save.boundingBox())!.height).toBe(clean);
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

  await page.goto("/settings/ai-review");
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

test("mobile: AI Review settings panel renders at phone width", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width layout check");
  await page.route("**/api/prefs", (r) =>
    r.fulfill({ json: { ai_review: AI_REVIEW } }),
  );
  await page.goto("/settings/ai-review");
  await expect(
    page.getByRole("heading", { name: "AI endpoint" }),
  ).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Model" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: /Prompts → Tail review/i }),
  ).toBeVisible();

  // The key row carries a field plus two actions (#834) — at phone width it must wrap
  // rather than squeeze the key readout to "*****…", and its buttons must clear the 44px
  // touch target (docs/design.md §8).
  const readout = page.getByText(/stored$/);
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
