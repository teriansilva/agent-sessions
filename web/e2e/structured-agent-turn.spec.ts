import { expect, test, type Page } from "@playwright/test";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

// #1438: a turn the agent runs for itself — a scheduled wake-up, a task notification — is shown
// as its own block with its latest call live, and Interrupt (or Esc) stops it.
const OWN = "33333333-4444-4555-8666-777777777777";
const LATEST = "curl -s https://git.example/api/v1/repos/x/issues/1436/comments";

type Json = Record<string, unknown>;

async function ownTurn(page: Page, theme = "dark", over: Json = {}, native: Json = {}) {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  const { state } = fixture;
  state.state = "idle";
  state.active_turn = null as unknown as string;
  state.turns = [
    { ...state.turns[0], state: "completed", reply: "Filed #1436. Waiting for Hermes' review." },
    {
      kind: "agent", turn_id: OWN, state: "running", reply: "", reply_truncated: false,
      ts: Date.parse("2026-10-14T06:41:00Z") / 1000, tools_truncated: false,
      tools: [
        { id: "t1", name: "ScheduleWakeup", summary: "270s · waiting for Hermes", outcome: "completed" },
        { id: "t2", name: "Bash", summary: "gh issue view 1436 --comments", outcome: "completed" },
        { id: "t3", name: "Read", summary: "web/src/components/NewSessionDialog.tsx", outcome: "completed" },
        { id: "t4", name: "Bash", summary: LATEST, outcome: "running" },
      ],
      ...over,
    },
  ];
  Object.assign(state.native, {
    background_active: true, background_turn: OWN, send_now_background: "interrupt",
    interrupt_own: true, ...native,
  });
  const interrupts: Json[] = [];
  let answer: { status: number; json: Json } = { status: 202, json: { handoff: "sent" } };
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/interrupt$`), (r) => {
    interrupts.push(r.request().postDataJSON());
    return r.fulfill(answer);
  });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-agent-turn")).toBeVisible();
  return { ...fixture, interrupts, answer: (a: typeof answer) => { answer = a; } };
}

const block = (page: Page) => page.getByTestId("structured-agent-turn");
const button = (page: Page) => page.getByTestId("structured-agent-interrupt");

for (const theme of ["dark", "light"]) {
  test(`#1438: the agent's own turn is collapsed with its latest call live, and expands to every call (${theme})`, async ({ page }, testInfo) => {
    const { state } = await ownTurn(page, theme);
    await expect(block(page)).toHaveAttribute("data-state", "running");
    await expect(block(page)).toContainText(/on its own/i);
    const summary = block(page).locator("summary");
    await expect(summary).toContainText("Working on its own · 4 commands & tools");
    await expect(page.getByTestId("structured-agent-latest")).toContainText("Bash");
    await expect(page.getByTestId("structured-agent-latest")).toContainText("issues/1436/comments");
    // Collapsed by default: the list is not shown until asked for.
    await expect(block(page).getByTestId("structured-tool").first()).toBeHidden();
    await page.screenshot({ path: testInfo.outputPath(`own-collapsed-${theme}.png`) });
    await summary.click();
    await expect(block(page).getByTestId("structured-tool")).toHaveCount(4);
    await page.screenshot({ path: testInfo.outputPath(`own-expanded-${theme}.png`) });
    // The old bare row is gone while the turn shows itself; nothing doubles it.
    await expect(page.getByTestId("structured-background-work")).toHaveCount(0);
    // It is not a message someone sent: no "you", no visitor name.
    await expect(block(page)).not.toContainText(/^you/i);
    // A new call shows up in the summary on the next read.
    (state.turns[1].tools as Json[]).push({ id: "t5", name: "Edit", summary: "docs/notes.md", outcome: "running" });
    state.revision++;
    await expect(page.getByTestId("structured-agent-latest")).toContainText("docs/notes.md");
  });
}

test("#1438: Interrupt stops the agent's own turn — the turn, by its target, not the worker", async ({ page }) => {
  const { interrupts, state } = await ownTurn(page);
  const box = await button(page).boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(44); // design §8
  await button(page).click();
  await expect.poll(() => interrupts.length).toBe(1);
  expect(interrupts[0].turn_id).toBe(OWN);
  expect(typeof interrupts[0].operation_id).toBe("string");
  // Acknowledged, not yet ended: it says so and takes no second press.
  await expect(button(page)).toHaveText("Interrupting…");
  await expect(button(page)).toBeDisabled();
  // The turn ends interrupted and stays as history.
  state.turns[1].state = "interrupted";
  state.native.background_turn = null;
  state.native.background_active = false;
  state.revision++;
  await expect(block(page)).toHaveAttribute("data-state", "interrupted");
  await expect(block(page).locator("summary")).toContainText("4 commands & tools · Interrupted");
  await expect(button(page)).toHaveCount(0);
});

test("#1438: Esc in the composer interrupts the agent's own turn, once", async ({ page }) => {
  const { interrupts } = await ownTurn(page);
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.press("Escape");
  await expect.poll(() => interrupts.length).toBe(1);
  expect(interrupts[0].turn_id).toBe(OWN);
  await composer.press("Escape"); // waiting on that turn: no second request
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(1);
});

test("#1438: Esc interrupts the operator's running turn the same way", async ({ page }) => {
  await mockStructuredGuidance(page, "claude-api", "interrupt");
  const interrupts: Json[] = [];
  await page.route(new RegExp(`/api/structured/sessions/claude-api(?::|%3A)${ID}/interrupt$`), (r) => {
    interrupts.push(r.request().postDataJSON());
    return r.fulfill({ status: 202, json: { handoff: "sent" } });
  });
  await page.goto(`/s/claude-api/${ID}`);
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.press("Escape");
  await expect.poll(() => interrupts.length).toBe(1);
  expect(interrupts[0].turn_id).toBe("11111111-2222-4333-8444-555555555555");
});

test("#1438: Esc that closes a menu interrupts nothing", async ({ page }) => {
  const { interrupts } = await ownTurn(page);
  const more = page.locator('button[aria-haspopup="menu"]:visible').first();
  await more.click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(0);
});

test("#1438: Esc while an IME is composing is typing, not an interrupt", async ({ page }) => {
  const { interrupts } = await ownTurn(page);
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.evaluate((el) =>
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true })),
  );
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(0);
});

test("#1438: a read-only session shows the turn but offers no Interrupt, and Esc does nothing", async ({ page }) => {
  const { interrupts, state } = await ownTurn(page);
  state.read_only = "its client was removed";
  state.revision++;
  await expect(page.getByTestId("structured-read-only")).toBeVisible();
  await expect(button(page)).toHaveCount(0);
  await block(page).locator("summary").press("Escape"); // focus inside the pane
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(0);
});

test("#1438: a turn that already ended says so instead of looping", async ({ page }) => {
  const { interrupts, answer } = await ownTurn(page);
  answer({ status: 409, json: { detail: "that turn is not active" } });
  await button(page).click();
  await expect.poll(() => interrupts.length).toBe(1);
  await expect(page.getByText("Interrupt not sent: that turn is not active")).toBeVisible();
  await page.waitForTimeout(1500);
  expect(interrupts).toHaveLength(1); // nothing retries on its own
});

test("#1438: an older worker or an engine that can't interrupt gets no button, and Esc does nothing", async ({ page }) => {
  const { interrupts } = await ownTurn(page, "dark", {}, { interrupt_own: undefined });
  await expect(button(page)).toHaveCount(0);
  await expect(page.getByTestId("structured-agent-no-interrupt")).toContainText("Stop ends the worker");
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.press("Escape");
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(0);
});

test("#1438: when the worker goes away the block settles and Esc stops nothing", async ({ page }) => {
  const { interrupts, state } = await ownTurn(page);
  state.turns[1].state = "failed";
  state.turns[1].reason = "the native worker ended during this turn";
  state.native.worker = null as unknown as string;
  state.native.background_turn = null;
  state.revision++;
  await expect(block(page)).toHaveAttribute("data-state", "failed");
  await expect(block(page)).toContainText("the native worker ended during this turn");
  await expect(button(page)).toHaveCount(0);
  const composer = page.getByRole("textbox").first();
  await composer.click();
  await composer.press("Escape");
  await page.waitForTimeout(300);
  expect(interrupts).toHaveLength(0);
});

test("#1438: a finished own turn keeps its reply, activity collapsed", async ({ page }) => {
  await ownTurn(page, "dark", {
    state: "completed",
    reply: "Hermes asked for a mobile mockup; I added it and edited the issue.",
  }, { background_turn: null, background_active: false });
  await expect(page.getByTestId("structured-agent-reply")).toContainText("mobile mockup");
  await expect(block(page).locator("summary")).toHaveText("4 commands & tools");
  await expect(button(page)).toHaveCount(0);
});

test("the agent's own turn has one left rule — the reply inside it draws none of its own", async ({ page }) => {
  await ownTurn(page, "dark", {
    state: "completed",
    reply: "Hermes asked for a mobile mockup; I added it and edited the issue.",
  }, { background_turn: null, background_active: false });
  const reply = page.getByTestId("structured-agent-reply");
  await expect(reply).toBeVisible();
  const style = await reply.evaluate((el) => {
    const s = getComputedStyle(el);
    return { width: s.borderLeftWidth, line: s.borderLeftStyle, pad: s.paddingLeft };
  });
  expect(style.line === "none" || style.width === "0px").toBe(true);
  expect(style.pad).toBe("0px");
  // Its text lines up with the block's header rather than sitting indented under a second rule.
  const head = await block(page).locator("> :first-child").boundingBox();
  const text = await reply.boundingBox();
  expect(Math.abs(text!.x - head!.x)).toBeLessThanOrEqual(1);
});

test("#1438: only a background task — a generic row, distinct from a turn, nothing to interrupt", async ({ page }) => {
  await ownTurn(page, "dark", { state: "completed" }, { background_turn: null, background_active: true });
  const row = page.getByTestId("structured-background-work");
  await expect(row).toContainText("A background task is running");
  await expect(row).toContainText("nothing to interrupt");
  await expect(row.getByRole("button")).toHaveCount(0);
  await expect(button(page)).toHaveCount(0);
});

for (const theme of ["dark", "light"]) {
  test(`#1438: the Esc hint shows only with a keyboard, and the new text reads clearly (${theme})`, async ({ page }, testInfo) => {
    await ownTurn(page, theme);
    const kbd = button(page).locator("kbd");
    if (testInfo.project.name === "mobile") {
      await expect(kbd).toBeHidden(); // touch: no key to press
    } else {
      await expect(kbd).toBeVisible();
      expect(await contrast(kbd)).toBeGreaterThanOrEqual(4.5);
    }
    expect(await contrast(page.getByTestId("structured-agent-latest"))).toBeGreaterThanOrEqual(4.5);
    expect(await contrast(block(page).locator("summary"))).toBeGreaterThanOrEqual(4.5);
  });
}

test("#1438: Esc with focus anywhere in the pane — not only the composer — interrupts", async ({ page }) => {
  const { interrupts } = await ownTurn(page);
  await block(page).locator("summary").press("Escape");
  await expect.poll(() => interrupts.length).toBe(1);
});

test("#1438: reduced motion keeps the agent turn's bars still", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await ownTurn(page);
  const motion = await block(page).locator("summary span[aria-hidden] span").first()
    .evaluate((el) => getComputedStyle(el).animationName);
  expect(motion).toBe("none");
});

/** WCAG contrast of an element's text against the first opaque background behind it. */
async function contrast(locator: ReturnType<Page["locator"]>): Promise<number> {
  return locator.evaluate((el) => {
    const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    let bg: number[] | null = null;
    for (let n: Element | null = el; n && !bg; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] === 1)) bg = c;
    }
    const fg = lum(rgb(getComputedStyle(el).color));
    const back = lum(bg ?? [255, 255, 255]);
    return (Math.max(fg, back) + 0.05) / (Math.min(fg, back) + 0.05);
  });
}
