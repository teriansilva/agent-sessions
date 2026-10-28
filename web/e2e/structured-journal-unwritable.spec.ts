import { expect, test, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// #1733: an API session's record rolls over invisibly; only when the rollover itself cannot be
// written (a full disk) does the operator see anything. Then a send is a KNOWN refusal before any
// claim (409 `journal_unwritable`): the draft stays in place, Send again is a NEW send, Hand off is
// offered second and may fail the same way — while a genuinely uncertain send still offers Retry.
// Real browser, desktop + mobile, both themes; the server is a small mocked state machine.

const A = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const ENGINE = "codex-api";
const ROUTE = new RegExp(`/api/structured/sessions/${ENGINE}(?::|%3A)([0-9a-f-]{36})(/[^?]+)?(\\?.*)?$`);
const DISK_FULL = "BattleLab couldn't write this session's record (disk full); nothing was sent";

type Json = Record<string, unknown>;
type Outcome = "record" | "lost" | "abort" | { status: number; json: Json };

const unwritable: Outcome = { status: 409, json: { detail: DISK_FULL, reason: "journal_unwritable" } };
const common = { reply: "", text_truncated: false, reply_truncated: false, tools_truncated: false,
  reason: null, tools: [] as Json[] };

async function app(page: Page, theme = "dark") {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "test", terminal_backend: "ws", auth_mode: "none", theme, overview_expanded: [], projects_hidden: [],
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await mockRoster(page);
  const s = {
    session_key: `${ENGINE}:${A}`, revision: 4, get event_cursor(): number { return this.revision; },
    cwd: "/home/u/demo", state: "idle", active_turn: null as string | null, model_requested: null,
    files: true, images: true, omitted_turns: 0, pending_requests: [] as Json[], read_only: null,
    native: { native_id: "native", worker: "worker", background_active: false },
    turns: [{ ...common, turn_id: "a0000000-0000-4000-8000-000000000000",
      operation_id: "a0000000-0000-4000-8000-000000000000", state: "completed",
      text: "Merge #1718 after checking its approval.",
      reply: "I'll merge #1718 after verifying its current approval and required checks." }] as Json[],
  };
  /** Every operation the server recorded (id → text), shown in a snapshot or not: a replay of
   *  any of them records nothing new. */
  const recorded = new Map<string, string>();
  const fx = { submits: [] as Json[], plan: [] as Outcome[], failReads: false, recorded };
  await page.route(ROUTE, async (r: Route) => {
    const [, , sub = ""] = r.request().url().match(ROUTE) ?? [];
    if (sub === "/turns" && r.request().method() === "POST") {
      const body = r.request().postDataJSON() as Json;
      fx.submits.push(body);
      const outcome = fx.plan.shift() ?? "record";
      if (outcome === "abort") return r.abort("failed");
      if (outcome !== "record" && outcome !== "lost") {
        return r.fulfill({ status: outcome.status, json: outcome.json });
      }
      const id = body.operation_id as string;
      if (outcome === "lost") {
        // Recorded, but its answer AND the next reads are lost, and no snapshot shows it yet:
        // its outcome stays unknown to the browser.
        recorded.set(id, body.text as string);
        fx.failReads = true;
        return r.abort("failed");
      }
      if (!recorded.has(id)) {  // a replay of an operation the server holds records nothing
        recorded.set(id, body.text as string);
        s.turns.push({ ...common, turn_id: id, operation_id: id,
          text: body.text, state: "running", ts: Date.now() / 1000 });
        s.state = "running";
        s.active_turn = id;
        s.revision++;
      }
      return r.fulfill({ status: 202, json: { state: "running", turn_id: body.operation_id,
        revision: s.revision } });
    }
    if (fx.failReads) return r.fulfill({ status: 503, json: { detail: "unavailable" } });
    if (sub === "/events") return r.fulfill({ json: { revision: s.revision, next_cursor: s.revision, events: [] } });
    if (!sub) return r.fulfill({ json: s });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return fx;
}

const box = (page: Page) => page.getByRole("textbox", { name: /^Message / });
const panel = (page: Page) => page.getByTestId("structured-unwritable");

async function open(page: Page) {
  await page.goto(`/s/${ENGINE}/${A}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(box(page)).toBeEnabled();
}

async function send(page: Page, text: string) {
  await box(page).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

/** sRGB relative luminance of a computed colour: `rgb()`/`rgba()` or `color(srgb …)`. */
function luminance(css: string): number {
  const srgb = css.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)/);
  const rgb = css.match(/rgba?\((\d+), (\d+), (\d+)/);
  const parts = srgb
    ? srgb.slice(1, 4).map(Number)
    : rgb
      ? rgb.slice(1, 4).map((v) => Number(v) / 255)
      : null;
  if (!parts) throw new Error(`unparsed colour ${css}`);
  const [r, g, b] = parts.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(fg: string, bg: string): number {
  const [a, b] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}

test("a send refused because the record can't be written keeps the draft; Send again is a NEW send", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push(unwritable, "record");
  await open(page);
  await send(page, "continue");
  const entry = page.getByTestId("structured-pending");
  await expect(panel(page)).toBeVisible();
  await expect(entry).toHaveAttribute("data-phase", "refused");
  await expect(panel(page)).toContainText("BattleLab can’t write this session right now");
  await expect(panel(page)).toContainText("The disk is full.");
  await expect(entry.getByTestId("structured-pending-state")).toHaveText(
    "Not sent — disk full, nothing was claimed");
  // The draft is kept in place — not moved back into the composer, not dropped.
  await expect(page.getByTestId("structured-unwritable-draft")).toHaveText("continue");
  await expect(box(page)).toHaveValue("");
  // A known refusal: no Retry. Send again and Hand off, each a ≥44 px target.
  await expect(entry.getByRole("button", { name: "Retry" })).toHaveCount(0);
  const again = panel(page).getByRole("button", { name: "Send again" });
  const handOff = panel(page).getByRole("button", { name: "Hand off to a new session" });
  for (const button of [again, handOff]) {
    const rect = await button.boundingBox();
    expect(rect?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.screenshot({ path: testInfo.outputPath("unwritable.png"), fullPage: true });
  // Nothing goes out behind it on its own.
  await page.waitForTimeout(1_200);
  expect(fx.submits).toHaveLength(1);
  await again.click();
  await expect.poll(() => fx.submits.length).toBe(2);
  expect(fx.submits[1].text).toBe("continue");
  expect(fx.submits[1].operation_id).not.toBe(fx.submits[0].operation_id); // a NEW operation
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn").filter({ hasText: "continue" })).toHaveCount(1);
});

test("Hand off may fail while the disk is full: its error shows and the draft stays", async ({ page }, testInfo) => {
  const fx = await app(page);
  fx.plan.push(unwritable);
  await page.route("**/api/handoff/prepare", (r) => r.fulfill({ json: {
    handle: "h-1", preview: "# Handoff", meta: { mode: "quick", turns: 1, bytes: 9, cap: 8192 } } }));
  let commits = 0;
  await page.route(/\/api\/handoff$/, (r) => {
    commits += 1;
    return r.fulfill({ status: 507, json: { detail: "the new session could not be written: no space left on device" } });
  });
  await open(page);
  const url = page.url();
  await send(page, "continue");
  await panel(page).getByRole("button", { name: "Hand off to a new session" }).click();
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /^hand off$/i }).click();
  await expect.poll(() => commits).toBe(1);
  await expect(dialog.getByRole("alert")).toContainText("no space left on device");
  await page.screenshot({ path: testInfo.outputPath("handoff-failed.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(page.url()).toBe(url); // no transfer was claimed
  await expect(page.getByTestId("structured-unwritable-draft")).toHaveText("continue");
  expect(fx.submits).toHaveLength(1);
});

test("a genuinely uncertain send still offers Retry, never the fallback", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push("abort");
  await open(page);
  await send(page, "continue");
  const entry = page.getByTestId("structured-pending");
  await expect(entry).toHaveAttribute("data-phase", "unknown");
  await expect(entry.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
});

for (const theme of ["dark", "light"]) {
  test(`the fallback reflows, shows focus and reads in the ${theme} theme`, async ({ page }, testInfo) => {
    const fx = await app(page, theme);
    fx.plan.push(unwritable);
    await open(page);
    await send(page, "continue — and then summarize what changed in the localization files");
    await expect(panel(page)).toBeVisible();
    // Phone reflow: nothing overflows sideways, the actions stack full width.
    const fits = await panel(page).evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
    expect(fits).toBe(true);
    const again = panel(page).getByRole("button", { name: "Send again" });
    const handOff = panel(page).getByRole("button", { name: "Hand off to a new session" });
    const [a, h] = [await again.boundingBox(), await handOff.boundingBox()];
    if (testInfo.project.name === "mobile") {
      expect(Math.abs((a?.width ?? 0) - (h?.width ?? 1))).toBeLessThan(1);
      expect((h?.y ?? 0)).toBeGreaterThan((a?.y ?? 0));
    }
    // Keyboard focus is visible on the primary action.
    await again.focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(again).toBeFocused();
    const outline = await again.evaluate((el) => getComputedStyle(el).outlineStyle);
    expect(outline).not.toBe("none");
    // Contrast: the title, the explanation and the refusal line against their background.
    const colours = await page.evaluate(() => {
      const panelEl = document.querySelector('[data-testid="structured-unwritable"]') as HTMLElement;
      const bg = getComputedStyle(panelEl).backgroundColor;
      const of = (sel: string) => getComputedStyle(document.querySelector(sel) as HTMLElement).color;
      return {
        bg,
        title: of('[data-testid="structured-unwritable"] > div:first-child'),
        text: of('[data-testid="structured-unwritable"] > p'),
      };
    });
    expect(contrast(colours.title, colours.bg)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(colours.text, colours.bg)).toBeGreaterThanOrEqual(4.5);
    await page.screenshot({ path: testInfo.outputPath(`fallback-${theme}.png`), fullPage: true });
  });
}

test("after Send again, a reload's Restore re-sends the NEW operation, never the refused one", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push(unwritable, "lost");
  await open(page);
  await send(page, "continue");
  await panel(page).getByRole("button", { name: "Send again" }).click();
  await expect.poll(() => fx.submits.length).toBe(2);
  const [refusedId, renewedId] = fx.submits.map((b) => b.operation_id as string);
  expect(renewedId).not.toBe(refusedId);
  await expect(page.getByTestId("structured-pending")).toHaveAttribute("data-phase", "unknown");
  // The persisted history already names the renewed send.
  const operations = await page.evaluate(
    () => (JSON.parse(localStorage.getItem("as:sent:v1") ?? "[]") as { operation?: string }[]).map((e) => e.operation),
  );
  expect(operations).toContain(renewedId);
  expect(operations).not.toContain(refusedId);
  fx.failReads = false;
  await page.reload();
  await expect(box(page)).toBeEnabled();
  await page.getByRole("button", { name: "Sent messages" }).click();
  await page.getByRole("button", { name: "Restore" }).first().click();
  await expect(box(page)).toHaveValue("continue");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => fx.submits.length).toBe(3);
  // The outcome was still unknown: Restore re-sends the SAME renewed operation, which the server
  // replays — never the refused id, never a fresh one. The words are recorded exactly once.
  expect(fx.submits[2].operation_id).toBe(renewedId);
  expect([...fx.recorded.values()].filter((text) => text === "continue")).toHaveLength(1);
});

test("if this device's sent history can't be rebound, Send again re-sends the never-claimed refused id", async ({ page }) => {
  const fx = await app(page);
  fx.plan.push(unwritable, "record");
  await open(page);
  await send(page, "continue");
  await expect(panel(page)).toBeVisible();
  // Storage now refuses every write to the sent history (quota, private mode).
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === "as:sent:v1") throw new DOMException("quota", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  await panel(page).getByRole("button", { name: "Send again" }).click();
  await expect.poll(() => fx.submits.length).toBe(2);
  // The history still names the refused id, so that is the id that goes: a Restore after a
  // reload re-sends the SAME operation, which the server replays — never a second turn.
  expect(fx.submits[1].operation_id).toBe(fx.submits[0].operation_id);
  const operations = await page.evaluate(
    () => (JSON.parse(localStorage.getItem("as:sent:v1") ?? "[]") as { operation?: string }[]).map((e) => e.operation),
  );
  expect(operations).toContain(fx.submits[0].operation_id);
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-turn").filter({ hasText: "continue" })).toHaveCount(1);
});
