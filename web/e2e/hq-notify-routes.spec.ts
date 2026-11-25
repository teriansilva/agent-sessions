/** Settings → AI → HQ connectors, a messenger's Notifications section (#1841, #1746 P3b), in a
 *  real browser on desktop and a phone, against a small stateful fake that answers as the server
 *  does: the identity it pins, its refusals in words, and the route's recent deliveries. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { settingsPath } from "../src/routes/settingsTabs";
import { mockShell } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

const TG = { id: "telegram", label: "Telegram", publisher: "battlelab", version: "1", category: "messenger", adapter: "telegram-bot", origin_mode: "fixed", fixed_origin: "https://api.telegram.org", auth: { kind: "path-token", scopes: ["bot-token"] }, capabilities: { read: [], send: ["message"] }, setup: "Create a bot." }; // prettier-ignore
const MX = { id: "matrix", label: "Matrix", publisher: "battlelab", version: "1", category: "messenger", adapter: "matrix-cs", origin_mode: "operator", fixed_origin: null, auth: { kind: "token-header", scopes: ["access-token"] }, capabilities: { read: [], send: ["message"] }, setup: "Sign in the bot." }; // prettier-ignore
const SMTP = { id: "smtp", label: "Email (SMTP)", publisher: "battlelab", version: "1", category: "messenger", adapter: "smtp", origin_mode: "operator", fixed_origin: null, auth: { kind: "basic", scopes: ["mail-password"] }, capabilities: { read: [], send: ["message"] }, setup: "Use your SMTP server." }; // prettier-ignore

const OFF = {
  enabled: false,
  destination: "",
  identity: null,
  classes: ["needs you", "mission escalation", "automation failed"],
  window_s: 60,
  quiet: null,
  cap_per_hour: 20,
  include_line: false,
  since: null,
  off: null,
};

function inst(manifest: string, over: Record<string, unknown> = {}) {
  return {
    id: "c_111111111111",
    manifest_id: manifest,
    origin: manifest === "smtp" ? "smtps://mail.example.com" : "https://api.telegram.org",
    status: "ready",
    token_set: true,
    account: manifest === "smtp" ? "me@example.com" : null,
    generation: 2,
    tested_generation: 2,
    tested: { login: manifest === "matrix" ? "@hq:example.org" : "hq_bot", repos: 0, at: 1 },
    last_test: { generation: 2, ok: true, code: null, at: 1 },
    enabled: { read: false },
    recipients_bound: true,
    recipients_chosen_for: "hq_bot",
    send: false,
    send_off: null,
    notify: { ...OFF },
    notify_ok: true,
    notify_why: null,
    created_at: 1,
    ...over,
  };
}

type Srv = {
  inst: ReturnType<typeof inst>;
  posts: Record<string, unknown>[];
  refuse: { status: number; detail: string } | null;
  deliveries: unknown[];
};

async function mock(page: Page, i: ReturnType<typeof inst>): Promise<Srv> {
  await mockShell(page);
  const srv: Srv = { inst: i, posts: [], refuse: null, deliveries: [] };
  await page.route(/\/api\/connectors(\/.*)?$/, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/deliveries"))
      return route.fulfill({ json: { deliveries: srv.deliveries } });
    if (path.endsWith("/notify")) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      srv.posts.push(body);
      if (srv.refuse)
        return route.fulfill({
          status: srv.refuse.status,
          json: { detail: srv.refuse.detail, code: "not_private" },
        });
      const rec = (srv.inst.recipients as { id: string; label: string }[]).find(
        (r) => r.id === body.destination_id,
      );
      srv.inst = {
        ...srv.inst,
        notify: {
          ...srv.inst.notify,
          enabled: body.enabled as boolean,
          destination: body.destination_id as string,
          identity: body.enabled
            ? { id: rec!.id, label: rec!.label, member: srv.inst.manifest_id === "matrix" ? "@marcus:example.org" : "" }
            : srv.inst.notify.identity,
          classes: body.classes as string[],
          window_s: body.window_s as number,
          quiet: body.quiet as null,
          cap_per_hour: body.cap_per_hour as number,
          include_line: body.include_line as boolean,
        },
      } as ReturnType<typeof inst>;
      return route.fulfill({ json: srv.inst });
    }
    return route.fulfill({
      json: { catalog: [TG, MX, SMTP], instances: [srv.inst], disclosure: "What HQ reads is sent to your AI endpoint." },
    });
  });
  return srv;
}

async function open(page: Page): Promise<Locator> {
  await page.goto(settingsPath("ai-hq-connectors"));
  const n = page.getByTestId("connector-notify");
  await expect(n).toBeVisible();
  return n;
}

async function checks(page: Page, scope: Locator) {
  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over).toBeLessThanOrEqual(0);
  if (!PHONE(page)) return;
  for (const el of await scope.locator("button:visible, input:visible, select:visible").all()) {
    const target =
      (await el.getAttribute("type")) === "checkbox"
        ? el.locator("xpath=ancestor::label[1]")
        : el;
    expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

test("Telegram: switch notifications on to your private chat, with quiet hours and a cap", async ({
  page,
}) => {
  const srv = await mock(
    page,
    inst("telegram", {
      recipients: [
        { id: "4211", label: "Marcus (@marcus_b)", kind: "private chat" },
        { id: "-1009", label: "Release crew", kind: "supergroup" },
      ],
    }),
  );
  const n = await open(page);
  // Only a private chat can carry notifications: the group is not offered.
  const dest = n.getByTestId("connector-notify-destination");
  await expect(dest.locator("option")).toHaveText(["Marcus (@marcus_b) · private chat"]);
  await expect(n.getByTestId("connector-notify-identity")).toContainText(
    "Telegram user Marcus (@marcus_b) (id 4211). Only switch this on if that’s you",
  );
  await n.getByTestId("connector-notify-enabled").check();
  await n.getByRole("checkbox", { name: "Automation failures" }).uncheck();
  await n.getByTestId("connector-notify-quiet").check();
  await n.getByLabel("Quiet hours start").fill("22:30");
  await n.getByTestId("connector-notify-cap").fill("5");
  await n.getByTestId("connector-notify-save").click();
  await expect.poll(() => srv.posts.length).toBe(1);
  const tz = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  expect(srv.posts[0]).toEqual({
    enabled: true,
    destination_id: "4211",
    classes: ["needs you", "mission escalation"],
    window_s: 60,
    quiet: { from: "22:30", to: "07:00", tz },
    cap_per_hour: 5,
    include_line: false,
  });
  // The page shows what the server answered: on, pinned to that chat.
  await expect(page.getByTestId("connector-notify-enabled")).toBeChecked();
  await expect(page.getByTestId("connector-notify-identity")).toContainText("id 4211");
  await checks(page, page.getByTestId("connector-notify"));
});

test("Matrix: a room of four is refused in the server's words, nothing switched on", async ({
  page,
}) => {
  const srv = await mock(
    page,
    inst("matrix", { recipients: [{ id: "!ops:example.org", label: "Ops", kind: "room" }] }),
  );
  srv.refuse = {
    status: 409,
    detail: "This room has 4 people in it. Notifications go only to a room of just you and the bot.",
  };
  const n = await open(page);
  await expect(n.getByTestId("connector-notify-identity")).toContainText(
    "checks that the room holds just the bot and one person",
  );
  await n.getByTestId("connector-notify-enabled").check();
  await n.getByTestId("connector-notify-save").click();
  await expect(n.getByTestId("connector-notify-error")).toHaveText(
    "This room has 4 people in it. Notifications go only to a room of just you and the bot.",
  );
  expect(srv.inst.notify.enabled).toBe(false);
  await checks(page, n);
});

test("mail: one typed address, and the copy says mail can't be taken back", async ({ page }) => {
  await mock(
    page,
    inst("smtp", {
      sender: "me@example.com",
      recipients: [{ id: "me@example.com", label: "Me", kind: "email" }],
    }),
  );
  const n = await open(page);
  await expect(n.getByTestId("connector-notify-identity")).toContainText(
    "Mail to me@example.com. BattleLab can’t verify who reads that address",
  );
  await expect(n.getByText(/Mail can’t be taken back/)).toBeVisible();
  await expect(n.getByText(/Send any message to your bot/)).toHaveCount(0);
  await checks(page, n);
});

test("why notify can't be switched on, and why it went off by itself", async ({ page }) => {
  await mock(
    page,
    inst("telegram", {
      recipients: [{ id: "4211", label: "Marcus (@marcus_b)", kind: "private chat" }],
      notify_ok: false,
      notify_why: "Set AGENT_SESSIONS_ORIGIN first: every notification carries a link into the app.",
      notify: { ...OFF, destination: "4211", off: { reason: "connector_changed", at: 1 } },
    }),
  );
  const n = await open(page);
  await expect(n.getByTestId("connector-notify-why")).toContainText("AGENT_SESSIONS_ORIGIN");
  await expect(n.getByTestId("connector-notify-off")).toContainText("this connector changed");
  await expect(n.getByTestId("connector-notify-enabled")).toBeDisabled();
});

test("recent deliveries in plain words, never the message text", async ({ page }) => {
  const srv = await mock(
    page,
    inst("telegram", {
      recipients: [{ id: "4211", label: "Marcus (@marcus_b)", kind: "private chat" }],
    }),
  );
  const now = Date.now() / 1000;
  const m = (label: string, sent = true, resolved = false) => ({ label, link: "/s/claude/x", sent, resolved });
  srv.deliveries = [
    { id: "r_1", state: "retracted", at: now - 60, settled_at: now, code: "", members: [m("Waiting for your approval", true, true)] },
    { id: "r_2", state: "delivered", at: now - 120, settled_at: now, code: "", members: [m("Asks you a question"), m("Mission escalation")] },
    { id: "r_3", state: "unconfirmed", at: now - 180, settled_at: now, code: "no_answer", members: [m("Automation failed")] },
    { id: "r_4", state: "skipped_quiet", at: now - 240, settled_at: now, code: "", members: [m("Stopped — take a look", false)] },
    { id: "r_5", state: "retract_evicted", at: now - 300, settled_at: now, code: "", members: [m("Waiting for your approval")] },
  ]; // prettier-ignore
  const n = await open(page);
  const rows = n.getByTestId("connector-notify-delivery");
  await expect(rows).toHaveCount(5);
  await expect(rows.nth(0)).toContainText("Sent · ✓ resolved");
  await expect(rows.nth(1)).toContainText("2 events · Asks you a question, Mission escalation");
  await expect(rows.nth(2)).toContainText("Not confirmed");
  await expect(rows.nth(2)).toContainText("it is not sent again");
  await expect(rows.nth(3)).toContainText("Skipped · quiet hours");
  await expect(rows.nth(4)).toContainText("No longer marked when handled");
  await checks(page, n);
});
