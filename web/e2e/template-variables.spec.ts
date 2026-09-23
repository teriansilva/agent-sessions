import { expect, test, type Page } from "@playwright/test";

// Real-browser checks for the templates' variables library (#1090, Phase 1). One variable is
// defined in the VARIABLES tab, two templates use it as a library field, and a send of EACH
// carries its value; then the variable is edited once and a send of each carries the new value —
// asserted on the exact bracketed-paste frames the composer writes, not on a DOM proxy. The API
// is an in-page stateful stub (POST/PATCH/DELETE really change what the next GET returns). Also:
// a missing variable blocks the send, a delete is refused while templates use it, and the tabs /
// rows are real touch targets on the phone. Runs on desktop AND mobile.

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
const VAR_LIMITS = { variables_max: 100, value_max: 2000, name_max: 32 };

const libField = { name: "host", label: "Host", default: "", required: false, source: "library" };
const TEMPLATES = [
  {
    id: "smoke",
    name: "Smoke test",
    description: "",
    tags: [],
    body: "Smoke test {{host}} now.",
    fields: [libField],
    images: [],
    created_at: 1,
    updated_at: 2,
    used_count: 0,
    last_used_at: null,
  },
  {
    id: "tail-logs",
    name: "Tail logs",
    description: "",
    tags: [],
    body: "Tail the logs on {{host}} for {{minutes}} minutes.",
    fields: [
      libField,
      { name: "minutes", label: "Minutes", default: "5", required: false, source: "template" },
    ],
    images: [],
    created_at: 1,
    updated_at: 2,
    used_count: 0,
    last_used_at: null,
  },
];

const RECORDING_WS = `
window.__sent = [];
window.__input = [];
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => {
      this.readyState = 1; this.onopen && this.onopen();
      const s = "\\x1b[?2004h\\x1b[H\\x1b[2Jready \\u276f ";
      this.onmessage && this.onmessage({ data: new TextEncoder().encode(s).buffer });
      this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: s.length }) });
    }, 20);
  }
  send(msg) {
    window.__sent.push(String(msg));
    try { const m = JSON.parse(msg); if (m && m.t === "i") window.__input.push(m.d); } catch {}
  }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

declare global {
  interface Window {
    __sent: string[];
    __input: string[];
  }
}

const ID = "11111111-2222-4333-8444-555555555555";

type Var = { name: string; value: string; created_at: number; updated_at: number };

/** The shell every page needs, plus a STATEFUL variables library. */
async function boot(page: Page) {
  const vars: Var[] = [];
  let clock = 100;
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
  await page.route(/\/api\/sessions\/[^/]+\/draft/, (r) =>
    r.fulfill({ json: { text: "", attachments: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/notifications**", (r) => r.fulfill({ json: { notifications: [] } }));
  await page.route(/\/api\/templates(\?.*)?$/, (r) =>
    r.fulfill({ json: { templates: TEMPLATES, limits: LIMITS } }),
  );
  await page.route(/\/api\/templates\/[^/]+\/used$/, (r) => r.fulfill({ json: TEMPLATES[0] }));
  const usedBy = (name: string) =>
    TEMPLATES.filter((t) => t.fields.some((f) => f.source === "library" && f.name === name)).map(
      (t) => ({ id: t.id, name: t.name }),
    );
  await page.route(/\/api\/template-variables(\/[^?]*)?(\?.*)?$/, async (r) => {
    const req = r.request();
    const url = new URL(req.url());
    const name = decodeURIComponent(url.pathname.split("/api/template-variables/")[1] ?? "");
    const method = req.method();
    if (method === "GET") {
      return r.fulfill({
        json: { variables: vars.map((v) => ({ ...v, used_by: usedBy(v.name) })), limits: VAR_LIMITS },
      });
    }
    if (method === "POST") {
      const body = req.postDataJSON() as { name: string; value: string };
      const rec = { ...body, created_at: ++clock, updated_at: clock };
      vars.push(rec);
      return r.fulfill({ status: 201, json: rec });
    }
    const v = vars.find((x) => x.name === name);
    if (!v) return r.fulfill({ status: 404, json: { detail: "unknown variable" } });
    if (method === "PATCH") {
      const body = req.postDataJSON() as { value: string; expected_updated_at: number };
      if (body.expected_updated_at !== v.updated_at) {
        return r.fulfill({ status: 409, json: { detail: "variable changed", current: v } });
      }
      v.value = body.value;
      v.updated_at = ++clock;
      return r.fulfill({ json: v });
    }
    // DELETE: refused while any template uses it — the server's rule.
    const deps = usedBy(name);
    if (deps.length) {
      return r.fulfill({
        status: 409,
        json: { detail: `${name} is still used by ${deps.length} templates`, dependants: deps },
      });
    }
    vars.splice(vars.indexOf(v), 1);
    return r.fulfill({ status: 204, body: "" });
  });
  await page.addInitScript(RECORDING_WS);
}

async function clickKey(page: Page, name: RegExp) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const chip = page.getByLabel(name);
    if (await chip.isVisible()) {
      try {
        await chip.click({ timeout: 2500 });
        return;
      } catch {
        /* moved into the overflow between the check and the click */
      }
    }
    const more = page.getByLabel(/more keys/i);
    if (await more.isVisible()) {
      await more.click();
      await page.getByRole("menu").waitFor({ state: "visible" });
      const item = page.getByRole("menuitem", { name });
      if ((await item.count()) > 0) {
        await item.click({ timeout: 2500 });
        return;
      }
      await page.keyboard.press("Escape");
      await page.getByRole("menu").waitFor({ state: "hidden" });
    }
    await page.waitForTimeout(300);
  }
  throw new Error(`key ${String(name)} never became clickable`);
}

/** Open the session, send one template from the picker, and return the paste it wrote. */
async function sendTemplate(page: Page, name: string): Promise<string> {
  await page.goto(`/s/claude/${ID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  // The socket has spoken (its first frame is out) before the picker is used — as in
  // templates-send.spec.ts; a send before then waits on readiness instead of being written.
  await expect.poll(() => page.evaluate(() => window.__sent.length)).toBeGreaterThan(0);
  await clickKey(page, /^use a template$/i);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: new RegExp(`^${name}`, "i") }).click();
  await expect(dialog.getByText(/host · from library/i)).toBeVisible();
  await dialog.getByRole("button", { name: new RegExp(`^send ${name}$`, "i") }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__input.at(-1))).toBe("\r");
  const frames = await page.evaluate(() => window.__input);
  const paste = frames.find((d) => d.startsWith("\x1b[200~"));
  expect(paste, "one bracketed paste").toBeTruthy();
  return paste!;
}

test("one variable feeds two templates, and one edit changes both sends", async ({ page }) => {
  await boot(page);

  // Define it in the VARIABLES tab.
  await page.goto("/templates?tab=variables");
  await expect(page.getByText(/no variables yet/i)).toBeVisible();
  await page.getByRole("button", { name: /new variable/i }).first().click();
  const form = page.getByRole("form", { name: /new variable/i });
  await form.getByPlaceholder("staging_host").fill("host");
  await form.getByPlaceholder("staging.acme.test").fill("a.test");
  await form.getByRole("button", { name: /^add$/i }).click();
  const row = page.locator('[data-variable="host"]');
  await expect(row).toContainText("a.test");
  await expect(row).toContainText("2 templates");

  expect(await sendTemplate(page, "Smoke test")).toBe("\x1b[200~Smoke test a.test now.\x1b[201~");
  expect(await sendTemplate(page, "Tail logs")).toBe(
    "\x1b[200~Tail the logs on a.test for 5 minutes.\x1b[201~",
  );

  // One edit…
  await page.goto("/templates?tab=variables");
  await page.getByRole("button", { name: /^edit host$/i }).click();
  await page.getByLabel(/value of host/i).fill("b.test");
  await page.getByRole("button", { name: /^save host$/i }).click();
  await expect(page.locator('[data-variable="host"]')).toContainText("b.test");

  // …reaches both templates at their next send.
  expect(await sendTemplate(page, "Smoke test")).toBe("\x1b[200~Smoke test b.test now.\x1b[201~");
  expect(await sendTemplate(page, "Tail logs")).toBe(
    "\x1b[200~Tail the logs on b.test for 5 minutes.\x1b[201~",
  );
});

test("a template whose library variable does not exist cannot be sent or inserted", async ({
  page,
}) => {
  await boot(page);
  await page.goto(`/s/claude/${ID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  await clickKey(page, /^use a template$/i);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: /^smoke test/i }).click();
  await expect(dialog.getByRole("alert")).toContainText("{{host}}");
  await expect(dialog.getByRole("button", { name: /^send smoke test$/i })).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: /^insert smoke test into composer$/i }),
  ).toBeDisabled();
  expect(await page.evaluate(() => window.__input.some((d) => d.includes("\x1b[200~")))).toBe(
    false,
  );
});

test("a delete is refused while templates use the variable, naming them", async ({ page }) => {
  await boot(page);
  await page.goto("/templates?tab=variables");
  await page.getByRole("button", { name: /new variable/i }).first().click();
  const form = page.getByRole("form", { name: /new variable/i });
  await form.getByPlaceholder("staging_host").fill("host");
  await form.getByPlaceholder("staging.acme.test").fill("a.test");
  await form.getByRole("button", { name: /^add$/i }).click();
  await page.getByRole("button", { name: /^delete host$/i }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^delete$/i }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText(/2 templates still use it/i);
  await expect(alert.getByRole("link", { name: "Tail logs" })).toHaveAttribute(
    "href",
    "/templates/tail-logs",
  );
  await expect(page.locator('[data-variable="host"]')).toBeVisible();
});

test("the tabs and a variable's actions are 44px touch targets on a phone", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "mobile", "touch-target sizes are the phone's contract");
  await boot(page);
  await page.goto("/templates?tab=variables");
  await page.getByRole("button", { name: /new variable/i }).first().click();
  const form = page.getByRole("form", { name: /new variable/i });
  await form.getByPlaceholder("staging_host").fill("host");
  await form.getByPlaceholder("staging.acme.test").fill("a.test");
  await form.getByRole("button", { name: /^add$/i }).click();
  for (const el of [
    page.getByRole("tab", { name: /templates/i }),
    page.getByRole("tab", { name: /variables/i }),
    page.getByRole("button", { name: /^edit host$/i }),
    page.getByRole("button", { name: /^delete host$/i }),
  ]) {
    const box = await el.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  // Nothing overflows the phone's width.
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
});
