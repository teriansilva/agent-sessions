import { expect, test, type Page } from "@playwright/test";

// Real-browser checks for SECRET template fields (#1090, Phase 2). A secret is stored from the
// VARIABLES tab and is never shown again; a template that uses it (plus a typed-once secret) is
// sent by the SERVER — asserted at the two boundaries that matter: NOT ONE bracketed paste goes
// through the browser's socket, and the send request carries the typed-once value but never the
// stored one (the browser does not have it). The sent history keeps the masked text. Insert is
// never offered for such a template. Runs on desktop AND mobile.

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

const TEMPLATE = {
  id: "migrate",
  name: "Run migration",
  description: "",
  tags: [],
  body: "Connect with {{db_pass}} and {{token}} for {{ticket}}.",
  fields: [
    {
      name: "db_pass",
      label: "DB password",
      default: "",
      required: false,
      source: "library",
      kind: "secret",
    },
    {
      name: "token",
      label: "Deploy token",
      default: "",
      required: true,
      source: "template",
      kind: "secret",
    },
    {
      name: "ticket",
      label: "Ticket",
      default: "",
      required: true,
      source: "template",
      kind: "text",
    },
  ],
  images: [],
  created_at: 1,
  updated_at: 2,
  used_count: 0,
  last_used_at: null,
};

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

const STORED = "stored-db-password-1";
const TYPED = "typed-deploy-token-1";

type Secret = {
  name: string;
  kind: "secret";
  set: true;
  needs_reentry: false;
  created_at: number;
  updated_at: number;
};

async function boot(page: Page) {
  const vars: Secret[] = [];
  const sends: unknown[] = [];
  const created: unknown[] = [];
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
    r.fulfill({ json: { templates: [TEMPLATE], limits: LIMITS } }),
  );
  await page.route(/\/api\/templates\/migrate\/send$/, (r) => {
    sends.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        masked: "Connect with [secret: db_pass] and [secret: token] for ACME-7.",
        template: TEMPLATE,
      },
    });
  });
  await page.route(/\/api\/template-variables(\/[^?]*)?(\?.*)?$/, (r) => {
    const req = r.request();
    if (req.method() === "POST") {
      const body = req.postDataJSON() as { name: string; kind: string; value: string };
      created.push(body);
      const rec: Secret = {
        name: body.name,
        kind: "secret",
        set: true,
        needs_reentry: false,
        created_at: 1,
        updated_at: 1,
      };
      vars.push(rec);
      return r.fulfill({ status: 201, json: rec });
    }
    return r.fulfill({
      json: {
        variables: vars.map((v) => ({ ...v, used_by: [{ id: "migrate", name: "Run migration" }] })),
        limits: { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8 },
      },
    });
  });
  await page.addInitScript(RECORDING_WS);
  return { sends, created };
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

test("a secret is stored write-only, and a template using it is sent by the server — never through the socket", async ({
  page,
}) => {
  const { sends, created } = await boot(page);

  // Store the secret.
  await page.goto("/templates?tab=variables");
  await page
    .getByRole("button", { name: /new secret/i })
    .first()
    .click();
  const form = page.getByRole("form", { name: /new secret/i });
  await form.getByPlaceholder("staging_host").fill("db_pass");
  const value = form.getByPlaceholder("stored encrypted");
  await expect(value).toHaveAttribute("type", "password");
  await value.fill(STORED);
  await form.getByRole("button", { name: /^add$/i }).click();
  const row = page.locator('[data-variable="db_pass"]');
  await expect(row).toContainText("never shown again");
  await expect(row).not.toContainText(STORED);
  expect(created).toEqual([{ name: "db_pass", value: STORED, kind: "secret" }]);

  // Send the template from a session.
  await page.goto(`/s/claude/${ID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__sent.length)).toBeGreaterThan(0);
  await clickKey(page, /^use a template$/i);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: /^run migration/i }).click();
  await expect(dialog.getByText("•••••••• stored")).toBeVisible();
  const token = dialog.getByLabel(/^deploy token$/i);
  await expect(token).toHaveAttribute("type", "password");
  await token.fill(TYPED);
  await dialog.getByLabel(/^ticket$/i).fill("ACME-7");
  await expect(dialog.getByLabel(/what will be sent/i)).toHaveText(
    "Connect with [secret: db_pass] and [secret: token] for ACME-7.",
  );
  await expect(
    dialog.getByRole("button", { name: /^insert run migration into composer$/i }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: /^send run migration$/i }).click();
  await expect(dialog).toBeHidden();

  // The request: the typed-once value, never the stored one (the browser does not have it).
  expect(sends).toHaveLength(1);
  expect(sends[0]).toEqual({
    session: `claude:${ID}`,
    values: { token: TYPED, ticket: "ACME-7" },
    expected_updated_at: 2,
  });
  expect(JSON.stringify(sends[0])).not.toContain(STORED);
  // Not one paste went through the browser's socket.
  const frames = await page.evaluate(() => window.__input);
  expect(frames.some((d) => d.includes("\x1b[200~"))).toBe(false);
  // The sent history keeps the masked text, and nothing typed survives in storage.
  const stored = await page.evaluate(() => JSON.stringify(localStorage));
  expect(stored).toContain("[secret: token]");
  expect(stored).not.toContain(TYPED);
  expect(stored).not.toContain(STORED);
});
