import { expect, test, type Page } from "@playwright/test";

// Real-browser checks for AI-suggested templates (#1090, Phase 3). The SUGGESTED tab starts empty
// and analyses NOTHING until ANALYSE is pressed (asserted on the network: zero suggest requests
// before the click); the drafts render; OPEN IN EDITOR lands in a new template's editor prefilled
// (and nothing is saved); ADD TO LIBRARY opens the variables form prefilled, and a credential-like
// suggestion opens NEW SECRET with an empty value. Real touch targets and no horizontal overflow
// on the phone. Runs on desktop AND mobile.

const LIMITS = {
  templates_max: 200,
  name_max: 120,
  description_max: 300,
  tags_max: 8,
  body_max: 100_000,
  fields_max: 12,
  label_max: 60,
  default_max: 500,
  images_max: 8,
  image_suffixes: [".png"],
};

const RESULT = {
  generated_at: 1,
  stats: { messages: 212, distinct: 90, sessions: 41, days: 30 },
  dropped: 0,
  suggestions: [
    {
      id: "aaaaaaaaaaaaaaaa",
      kind: "template",
      name: "Fix review notes",
      reason: "You sent a variant of this 17 times across 9 sessions.",
      count: 17,
      body: "Read the latest Hermes review on {{pr}}, apply every mechanical note, run the tests, push.",
      fields: [{ name: "pr", label: "PR", default: "" }],
    },
    {
      id: "bbbbbbbbbbbbbbbb",
      kind: "variable",
      name: "staging_host",
      reason: "staging.acme.test appears in 23 of your messages.",
      count: 23,
      value: "staging.acme.test",
      secret: false,
    },
    {
      id: "cccccccccccccccc",
      kind: "variable",
      name: "deploy_token",
      reason: "A token you paste by hand.",
      count: 4,
      value: "",
      secret: true,
    },
  ],
};

async function boot(page: Page) {
  const calls = { suggest: 0, dismiss: [] as string[], createTemplate: 0, write: [] as string[] };
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/notifications**", (r) => r.fulfill({ json: { notifications: [] } }));
  await page.route(/\/api\/templates(\?.*)?$/, (r) => {
    if (r.request().method() === "POST") {
      calls.createTemplate += 1;
    }
    return r.fulfill({ json: { templates: [], limits: LIMITS } });
  });
  await page.route(/\/api\/template-variables(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        variables: [],
        limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8 },
      },
    }),
  );
  let analysed = false;
  await page.route(/\/api\/templates\/suggestions$/, (r) =>
    r.fulfill({ json: { result: analysed ? RESULT : null, configured: true } }),
  );
  await page.route(/\/api\/templates\/suggest$/, (r) => {
    calls.suggest += 1;
    analysed = true;
    return r.fulfill({ json: RESULT });
  });
  await page.route(/\/api\/templates\/suggestions\/[0-9a-f]+\/dismiss$/, (r) => {
    calls.dismiss.push(r.request().url().split("/").at(-2)!);
    return r.fulfill({ status: 204, body: "" });
  });
  await page.route(/\/api\/templates\/write$/, (r) => {
    calls.write.push((r.request().postDataJSON() as { request: string }).request);
    return r.fulfill({
      json: {
        template: {
          name: "Review a PR",
          description: "Review a pull request against the guidelines.",
          body: "Review PR {{pr}} against {{guidelines_url}} and list every issue.",
          fields: [
            { name: "pr", label: "PR", default: "", source: "template", kind: "text" },
            { name: "guidelines_url", label: "Guidelines", default: "", source: "library", kind: "text" },
          ],
        },
      },
    });
  });
  return calls;
}

test("nothing is analysed until ANALYSE; the drafts open in the editor and the library prefilled", async ({
  page,
}) => {
  const calls = await boot(page);
  await page.goto("/templates?tab=suggested");
  await expect(page.getByText(/what template should you write/i)).toBeVisible();
  expect(calls.suggest).toBe(0);

  await page.getByRole("button", { name: /analyse my messages/i }).click();
  const list = page.getByRole("list", { name: /^suggestions$/i });
  await expect(list.getByRole("listitem")).toHaveCount(3);
  expect(calls.suggest).toBe(1);
  await expect(page.getByText(/looks like a credential/i)).toBeVisible();

  // OPEN IN EDITOR: a NEW template, prefilled — nothing saved.
  await page.getByRole("button", { name: /open fix review notes in the editor/i }).click();
  await expect(page).toHaveURL(/\/templates\/new$/);
  await expect(page.getByLabel(/^name/i)).toHaveValue("Fix review notes");
  await expect(page.getByLabel(/field 1 name/i)).toHaveValue("pr");
  expect(calls.createTemplate).toBe(0);

  // ADD TO LIBRARY: the variables form, prefilled.
  await page.goto("/templates?tab=suggested");
  await page.getByRole("button", { name: /add staging_host to the library/i }).click();
  const form = page.getByRole("form", { name: /new variable/i });
  await expect(form.getByPlaceholder("staging_host")).toHaveValue("staging_host");
  await expect(form.getByPlaceholder("staging.acme.test")).toHaveValue("staging.acme.test");

  // A credential-like suggestion: NEW SECRET, the value empty — never echoed back.
  await page.goto("/templates?tab=suggested");
  await page.getByRole("button", { name: /add deploy_token to the library/i }).click();
  const secret = page.getByRole("form", { name: /new secret/i });
  await expect(secret.getByPlaceholder("staging_host")).toHaveValue("deploy_token");
  await expect(secret.getByPlaceholder("stored encrypted")).toHaveValue("");

  // DISMISS.
  await page.goto("/templates?tab=suggested");
  await page.getByRole("button", { name: /dismiss staging_host/i }).click();
  await expect(list.getByRole("listitem")).toHaveCount(2);
  expect(calls.dismiss).toEqual(["bbbbbbbbbbbbbbbb"]);
});

test("the suggestion cards are real touch targets and never overflow the phone", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "mobile", "touch-target sizes are the phone's contract");
  await boot(page);
  await page.goto("/templates?tab=suggested");
  await page.getByRole("button", { name: /analyse my messages/i }).click();
  await expect(
    page.getByRole("list", { name: /^suggestions$/i }).getByRole("listitem"),
  ).toHaveCount(3);
  for (const el of [
    page.getByRole("tab", { name: /suggested/i }),
    page.getByRole("button", { name: /open fix review notes in the editor/i }),
    page.getByRole("button", { name: /dismiss fix review notes/i }),
    page.getByRole("button", { name: /analyse again/i }),
  ]) {
    expect((await el.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
});

test("every Templates tab fits a 320px phone — measured against the tab row, not the document (#1110 review)", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "mobile", "the narrow-phone reflow is the phone's contract");
  await page.setViewportSize({ width: 320, height: 640 });
  await boot(page);
  await page.goto("/templates?tab=suggested");
  const row = page.getByRole("tablist", { name: /templates sections/i });
  await expect(row.getByRole("tab")).toHaveCount(3);
  // The page hides horizontal overflow, so the document never widens: a clipped tab only shows
  // as a tab box running past the row that holds it.
  const box = (await row.boundingBox())!;
  for (const tab of await row.getByRole("tab").all()) {
    const t = (await tab.boundingBox())!;
    expect(t.x + t.width, await tab.textContent()).toBeLessThanOrEqual(box.x + box.width + 0.5);
    expect(t.height).toBeGreaterThanOrEqual(44);
  }
});

test("WRITE ME A TEMPLATE FOR… works without any analysis and lands in the editor (#1110)", async ({
  page,
}, info) => {
  const calls = await boot(page);
  await page.goto("/templates?tab=suggested");
  const box = page.getByLabel(/write me a template for/i);
  await expect(box).toBeVisible();
  await box.fill("reviewing a pull request against our guidelines");
  const go = page.getByRole("button", { name: /write it/i });
  if (info.project.name === "mobile") {
    expect((await go.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    expect((await box.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  await go.click();
  await expect(page).toHaveURL(/\/templates\/new$/);
  await expect(page.getByLabel(/^name/i)).toHaveValue("Review a PR");
  expect(calls.write).toEqual(["reviewing a pull request against our guidelines"]);
  expect(calls.suggest).toBe(0);
  expect(calls.createTemplate).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
});
