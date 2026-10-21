import { expect, test, type Page } from "@playwright/test";

// Real-browser checks for TEMPLATES P3 (#905): a template is sent into a session from the
// composer's picker, as ONE message through the composer's own delivery sequence. The socket is
// stubbed so the exact input frames can be asserted — a clear, one bracketed paste carrying the
// substituted body plus the image paths, and one deferred Enter — and the `/used` bump is
// observed to fire only after that Enter. Runs on desktop AND mobile.

const UP = "/home/u/.agent-sessions/uploads";
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
  id: "pr-review",
  name: "PR review checklist",
  description: "Review a PR against the checklist.",
  tags: ["review"],
  body: "Review PR {{pr_url}} for {{issue_ref}}.",
  fields: [
    { name: "pr_url", label: "PR link", default: "", required: true },
    { name: "issue_ref", label: "Issue", default: "the linked issue", required: false },
  ],
  images: [{ name: "shot.png", path: `${UP}/20260903-101512-shot.png` }],
  created_at: 1_788_400_000,
  updated_at: 1_788_430_000.5,
  used_count: 3,
  last_used_at: null,
};

// Records every `{t:"i"}` frame the app sends (the compose-sent-history spec's stub). `__drop`
// makes the socket go non-OPEN once it has seen the bracketed paste, so the deferred Enter finds
// a dead socket — the one way a send legitimately ends up not delivered.
const RECORDING_WS = (dropAfterPaste: boolean) => `
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
    try {
      const m = JSON.parse(msg);
      if (m && m.t === "i") {
        window.__input.push(m.d);
        if (${dropAfterPaste} && m.d.indexOf("\\u001b[200~") !== -1) this.readyState = 3;
      }
    } catch {}
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

async function mockShell(page: Page) {
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
}

async function mockTemplates(page: Page) {
  const used: { at: number; inputsAtCall: number }[] = [];
  await page.route(/\/api\/templates(\?.*)?$/, (r) =>
    r.fulfill({ json: { templates: [TEMPLATE], limits: LIMITS } }),
  );
  await page.route(/\/api\/templates\/[^/]+\/used$/, async (r) => {
    // Record how many input frames had been sent when the bump arrived: it must be after the "\r".
    const n = await r.request().frame().evaluate(() => window.__input.length);
    used.push({ at: Date.now(), inputsAtCall: n });
    return r.fulfill({ json: { ...TEMPLATE, used_count: TEMPLATE.used_count + 1 } });
  });
  return used;
}

async function boot(page: Page, dropAfterPaste = false) {
  await mockShell(page);
  const used = await mockTemplates(page);
  await page.addInitScript(RECORDING_WS(dropAfterPaste));
  await page.goto(`/s/claude/${ID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__sent.length)).toBeGreaterThan(0);
  return used;
}

// A key-bar chip may be inline or collapsed into the "…" overflow (KeyBar re-measures as the
// composer grows, so a chip can move there right after it appears). Try the overflow first when
// it exists, then the inline chip — matching on the stable aria-label either way.
async function clickKey(page: Page, name: RegExp) {
  // KeyBar re-measures after a chip appears, so a chip can be inline on one frame and in the
  // overflow on the next. Try inline, then the overflow, a few times, with short waits.
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
      await page.getByRole("menu").waitFor({ state: "visible" }); // `count()` does not auto-wait
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

async function openTemplates(page: Page) {
  await clickKey(page, /^use a template$/i);
  await expect(page.getByRole("dialog")).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  // Nothing here is shared; each test boots its own page.
  void page;
});

test("SEND from the picker pastes the substituted template as one message and bumps /used only after the Enter", async ({
  page,
}) => {
  const used = await boot(page);
  await openTemplates(page);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: /^pr review checklist/i }).click();
  const send = dialog.getByRole("button", { name: /^send pr review checklist$/i });
  await expect(send).toBeDisabled();
  await dialog.getByLabel(/^pr link$/i).fill("https://x/pulls/1");
  await expect(send).toBeEnabled();
  await send.click();
  await expect(dialog).toBeHidden();

  // The exact frames: clear, ONE bracketed paste with the substituted body + the image path,
  // then the deferred Enter.
  await expect.poll(() => page.evaluate(() => window.__input.at(-1))).toBe("\r");
  const frames = await page.evaluate(() => window.__input);
  expect(frames).toEqual([
    "\x01\x0b",
    `\x1b[200~Review PR https://x/pulls/1 for the linked issue. ${UP}/20260903-101512-shot.png\x1b[201~`,
    "\r",
  ]);
  // The usage bump fired exactly once, after every frame had gone out.
  await expect.poll(() => used.length).toBe(1);
  expect(used[0].inputsAtCall).toBe(3);
  // It lands in the sent history like any other send.
  await page.getByLabel(/sent messages/i).or(page.getByLabel(/more keys/i)).first().click();
  if (!(await page.getByRole("dialog").isVisible())) {
    await page.getByRole("menuitem", { name: /sent messages/i }).click();
  }
  await expect(page.getByRole("dialog")).toContainText("Review PR https://x/pulls/1");
});

test("a dropped Enter leaves the template unconfirmed and never bumps /used", async ({ page }) => {
  const used = await boot(page, true);
  await openTemplates(page);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: /^pr review checklist/i }).click();
  await dialog.getByLabel(/^pr link$/i).fill("https://x/pulls/2");
  await dialog.getByRole("button", { name: /^send pr review checklist$/i }).click();
  await expect
    .poll(() => page.evaluate(() => window.__input.some((d) => d.includes("\x1b[200~"))))
    .toBe(true);
  await expect(page.getByText(/not sent/i)).toBeVisible();
  await page.waitForTimeout(400);
  expect(used).toHaveLength(0);
  const frames = await page.evaluate(() => window.__input);
  expect(frames.filter((d) => d === "\r")).toHaveLength(0);
});

test("INSERT fills the composer with the substituted body and the image pill; nothing is sent", async ({
  page,
}) => {
  const used = await boot(page);
  const before = await page.evaluate(() => window.__input.length);
  await openTemplates(page);
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: /^pr review checklist/i }).click();
  await dialog.getByLabel(/^pr link$/i).fill("https://x/pulls/3");
  await dialog.getByRole("button", { name: /insert pr review checklist into composer/i }).click();
  await expect(dialog).toBeHidden();
  const ta = page.getByPlaceholder(/type here/i);
  await expect(ta).toHaveValue("Review PR https://x/pulls/3 for the linked issue.");
  await expect(page.getByTitle(`${UP}/20260903-101512-shot.png`)).toBeVisible();
  expect(await page.evaluate(() => window.__input.length)).toBe(before);
  expect(used).toHaveLength(0);
});

test("Save as template from the composer lands in the editor prefilled", async ({ page }) => {
  await boot(page);
  const ta = page.getByPlaceholder(/type here/i);
  if (!(await ta.isVisible())) await page.getByLabel(/open compose box/i).click();
  await ta.fill("Keep this one around");
  await clickKey(page, /^save as template$/i);
  await expect(page).toHaveURL(/\/templates\/new$/);
  await expect(page.getByLabel(/instructions/i)).toHaveValue("Keep this one around");
  await expect(page.getByText(/prefilled from a sent message/i)).toBeVisible();
});

test("USE on a gallery card picks a session, lands in its pane, and opens the picker on that template", async ({
  page,
}) => {
  await mockShell(page);
  await mockTemplates(page);
  // The sidebar's store holds one session — the chooser lists exactly what the sidebar loaded.
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: `claude:${ID}`,
            engine: "claude",
            uuid: ID,
            short_uuid: ID.slice(0, 8),
            cwd: "/home/u/proj",
            project: { kind: "folder", id: "/home/u/proj", name: "proj" },
            last_mtime: 1_788_400_000,
            working: false,
            first_user_message: "fix the thing",
            title: "Fix the thing",
            sticky: false,
            archived: false,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.addInitScript(RECORDING_WS(false));
  await page.goto("/templates");
  await page.getByRole("button", { name: /use pr review checklist/i }).click();
  const chooser = page.getByRole("dialog");
  await expect(chooser).toContainText(/pick a session/i);
  await chooser.getByRole("button", { name: /use in fix the thing/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/claude/${ID}$`));
  const picker = page.getByRole("dialog");
  await expect(picker).toContainText(/pick one/i);
  await expect(picker.getByRole("button", { name: /^pr review checklist/i })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  // Nothing was sent by arriving here.
  await expect(page.locator(".xterm")).toBeVisible();
  expect(await page.evaluate(() => window.__input.length)).toBe(0);
  // The staging was consumed: a reload lands with nothing open.
  await page.keyboard.press("Escape");
  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.getByRole("dialog")).toBeHidden();
});

test("closing a picker opened from the key bar's overflow returns focus to the More-keys trigger, not document.body (#908 round 7)", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 }); // narrow enough that the chip lives in the "…" overflow
  await boot(page);
  const more = page.getByLabel(/more keys/i);
  await expect(more).toBeVisible();
  await more.click();
  await page.getByRole("menu").waitFor({ state: "visible" });
  await page.getByRole("menuitem", { name: /^use a template$/i }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  // The menu item that opened it unmounted on click; focus must land on something connected.
  await expect
    .poll(() =>
      page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? document.activeElement?.tagName ?? "none"),
    )
    .toBe("More keys");
});
