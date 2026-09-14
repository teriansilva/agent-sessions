import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";
// The REAL validator, imported rather than re-implemented — a second copy of the grammar in the
// test would only ever prove the two copies agree with each other.
import {
  isTermFontFamily,
  TERM_FONT_FAMILY_MAX_LEN,
} from "../src/theme/termFont";

// The same table the pytest and Vitest suites read. Here it is checked against the browser's
// OWN CSS parser, which is the only authority on whether a stack the operator typed will
// actually be applied (#868 review).
const FIXTURE = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../tests/fixtures/term_font_family_cases.json"),
    "utf8",
  ),
) as { write_accepted: string[]; browser_rejects: string[] };

// #866: the terminal FACE is the second axis beside the size (#859). Same reason these run in a
// real browser rather than jsdom: what is under test is layout arithmetic xterm does against
// measured glyph metrics, and jsdom reports no font metrics at all — a "the font changed" unit
// test there would pass against any implementation, including none.
//
// The metric-changing test face is the generic `sans-serif`, deliberately. It is a terrible
// terminal font and a perfect test fixture: it resolves on every machine with no vendored asset,
// and it is proportional, so its cell measurement cannot coincide with the monospace default the
// way two mono faces easily can. A test face that happened to match the default would make the
// column assertion below pass while proving nothing.

const RECORDING_WS = `
window.__ws = { count: 0, sent: [] };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    window.__ws.count++;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(data) { try { window.__ws.sent.push(JSON.parse(data)); } catch { /* binary */ } }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

async function mockApi(page: Page, termFontFamily?: string) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        term_font_size: 13,
        ...(termFontFamily ? { term_font_family: termFontFamily } : {}),
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
}

/** Every cols value the client has told the pty about, oldest first. */
function resizes(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    (
      window as unknown as { __ws: { sent: { t: string; cols?: number }[] } }
    ).__ws.sent
      .filter((m) => m.t === "r" && typeof m.cols === "number")
      .map((m) => m.cols as number),
  );
}

/** The face the LIVE terminal is actually rendering in.
 *
 *  Read off **`.xterm-rows`**, which is the element xterm applies `options.fontFamily` to — NOT
 *  off `.xterm`, and not off our own React state. Both alternatives are traps this test fell
 *  into before it was red-proofed: `.xterm` carries no font of its own and simply inherits the
 *  app's body stack, which itself *ends in `sans-serif`* — so a `toBe("sans-serif")`
 *  assertion against it passed against a deliberately broken build that never applied the
 *  operator's face at all. Hence `.xterm-rows`, and hence exact equality at every call site. */
function terminalFace(page: Page): Promise<string> {
  return page.evaluate(() => {
    const rows = document.querySelector(".xterm-rows");
    return rows ? getComputedStyle(rows).fontFamily : "";
  });
}

/** The app chrome's own font — what `.xterm-rows` would report if the face never reached xterm
 *  and it fell back to inheritance. Asserted against, so "not the default" is proven. */
function chromeFace(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).fontFamily);
}

/** Pick a face through the shipped Settings UI: the Custom card, then the stack field. */
async function chooseCustomFace(page: Page, stack: string) {
  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  const field = page.getByLabel("Custom font stack");
  await field.fill(stack);
  await field.press("Enter");
}

const FIRA_PRESET =
  '"Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

/** Hold /api/config until the test says so, then seed `family`.
 *
 *  A fixed sleep is NOT enough and the review proved it: with a `setTimeout(1200)` the response
 *  can win on a loaded runner, the test then does its editing AFTER the seed has landed, and it
 *  passes against the very defect it exists to catch (demonstrated: the old unconditional
 *  `setCustomOpen` restored, the test still green 2/2). Gating on an explicit release makes the
 *  ordering a fact rather than a hope. */
async function seedLate(page: Page, family: string) {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route("**/api/config", async (r) => {
    await gate;
    await r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        term_font_size: 13,
        term_font_family: family,
      },
    });
  });
  return {
    /** Release the response and wait until the provider has actually applied it — asserted on
     *  the device cache it writes, so "the seed landed" is observed, never assumed. */
    async land() {
      release();
      await expect
        .poll(() => page.evaluate(() => localStorage.getItem("tr-termfont")))
        .toBe(family);
    },
  };
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(RECORDING_WS);
});

test("the face an operator picks is the face the AGENT gets — on every engine (#866)", async ({
  page,
}) => {
  // The "it should work for all agents" claim, asserted rather than assumed: two different
  // engines, one face. There is deliberately no per-engine override to get wrong.
  await chooseCustomFace(page, "sans-serif");

  for (const route of ["/s/claude/font-all-1", "/s/opencode/font-all-2"]) {
    await page.goto(route);
    await expect(page.locator(".xterm")).toBeVisible();
    // RED before this change: the face is a hard-coded constant, so it is the monospace stack
    // here whatever the operator picked.
    await expect.poll(() => terminalFace(page)).toBe("sans-serif");
    // …and provably not the inherited chrome font, which also ends in `sans-serif`.
    expect(await terminalFace(page)).not.toBe(await chromeFace(page));
    expect(await terminalFace(page)).not.toContain("ui-monospace");
  }
});

test("a wider face costs the agent COLUMNS — the face is layout, not decoration (#866)", async ({
  page,
}) => {
  // The #859 property, one axis over: the size decides how many columns the agent lays out
  // against, and so does the face. Asserted on the `{t:"r",cols}` frame the client actually
  // sends to the pty — the thing that SIGWINCHes the agent — not on a DOM proxy for it.
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.goto("/s/claude/font-cols-mono");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(async () => (await resizes(page)).length).toBeGreaterThan(0);
  const monoCols = (await resizes(page)).at(-1) as number;
  expect(monoCols).toBeGreaterThan(0);

  await chooseCustomFace(page, "sans-serif");

  await page.goto("/s/claude/font-cols-sans");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(async () => (await resizes(page)).length).toBeGreaterThan(0);
  const sansCols = (await resizes(page)).at(-1) as number;

  // A proportional face measures a wider cell, so the same pixel width holds fewer columns.
  expect(sansCols).toBeLessThan(monoCols);
});

test("arriving in a chosen face settles on ONE width, not a march (#866)", async ({
  page,
}) => {
  // The #227/#349 guard, in the shape this axis can actually produce it. The face is applied
  // through the same debounced `refitSoonRef` the size uses, so a fresh mount must converge on
  // a single width rather than walking the agent through intermediate ones.
  //
  // DISTINCT widths, not frames: a single settle legitimately emits two or three frames at the
  // SAME cols as the row count resolves under the ResizeObserver (pre-existing, unrelated).
  await page.setViewportSize({ width: 1280, height: 900 });
  await chooseCustomFace(page, "sans-serif");

  await page.goto("/s/claude/font-settle");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(async () => (await resizes(page)).length).toBeGreaterThan(0);
  await page.waitForTimeout(700); // debounce + settle, with headroom on a loaded runner

  const distinct = new Set(await resizes(page));
  expect(distinct.size).toBe(1);
  // …and exactly one socket: applying a face must never rebuild the terminal.
  expect(
    await page.evaluate(
      () => (window as unknown as { __ws: { count: number } }).__ws.count,
    ),
  ).toBe(1);
});

test("the face is per DEVICE and survives a reload (#866)", async ({ page }) => {
  await chooseCustomFace(page, "sans-serif");
  await page.goto("/s/claude/font-reload");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");

  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");
});

test("the device's choice beats the server's seed (#866)", async ({ page }) => {
  // "Saved per device" is the whole reason this is one pref instead of two: a phone parked on
  // one face is never overwritten by whatever the desktop last wrote.
  await chooseCustomFace(page, "sans-serif");
  await mockApi(page, '"Fira Code", ui-monospace, monospace'); // a different server value
  await page.goto("/s/claude/font-device-wins");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");
  expect(await terminalFace(page)).not.toContain("Fira Code");
});

test("a face this device does not have is offered as unavailable, not silently substituted (#866)", async ({
  page,
}) => {
  await page.goto(settingsPath("appearance"));
  // System is whatever the device ships — always selectable.
  await expect(page.getByRole("radio", { name: /^System/ })).toBeEnabled();
  // SF Mono is an Apple face: absent on the Linux runners this suite runs on. Without the
  // primary-family check it would look selectable and then render identically to System,
  // because every preset stack ends in `monospace` and that always resolves.
  await expect(page.getByRole("radio", { name: /^SF Mono/ })).toBeDisabled();
});

test("cards and the stack field are keyboard-operable, and meet the 44px floor (#866)", async ({
  page,
}) => {
  await page.goto(settingsPath("appearance"));
  const system = page.getByRole("radio", { name: /^System/ });
  const box = await system.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);

  // Reachable and operable without a pointer: focus the Custom card and activate it with the
  // keyboard, then type a stack and commit with Enter.
  const custom = page.getByRole("radio", { name: /^Custom/ });
  await custom.focus();
  await expect(custom).toBeFocused();
  await page.keyboard.press("Enter");
  const field = page.getByLabel("Custom font stack");
  await expect(field).toBeVisible();
  await field.fill("sans-serif");
  await field.press("Enter");

  await page.goto("/s/claude/font-kbd");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");
});

test("a maximum-length custom stack does not widen the panel on a phone (#866)", async ({
  page,
}) => {
  await page.setViewportSize({ width: 412, height: 900 });
  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  const field = page.getByLabel("Custom font stack");
  // Exactly the cap (120), all legal characters — the longest thing the server will accept.
  await field.fill("Menlo, " + "a".repeat(113));
  await field.press("Enter");

  // The document must not scroll horizontally: a long stack scrolls INSIDE its input.
  const overflow = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    win: window.innerWidth,
  }));
  expect(overflow.doc).toBeLessThanOrEqual(overflow.win);
});

test("choosing a preset closes the custom field, so one value has one view (#866)", async ({
  page,
}) => {
  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  await expect(page.getByLabel("Custom font stack")).toBeVisible();

  await page.getByRole("radio", { name: /^System/ }).click();
  // A stale draft left open would let the next Enter silently overwrite the preset just picked.
  await expect(page.getByLabel("Custom font stack")).toHaveCount(0);
  await expect(page.getByRole("radio", { name: /^System/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

test("an unusable stack is refused with a reason, and the live face is untouched (#866)", async ({
  page,
}) => {
  await chooseCustomFace(page, "sans-serif");
  await page.goto(settingsPath("appearance"));
  const field = page.getByLabel("Custom font stack");
  await field.fill('"Fira Code'); // all-legal characters, unbalanced quote, renders as nothing
  await field.press("Enter");
  await expect(page.getByRole("alert")).toBeVisible();

  await page.goto("/s/claude/font-bad-stack");
  await expect(page.locator(".xterm")).toBeVisible();
  // The refused entry must not have landed anywhere — the previous choice still stands.
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");
});

test("every stack the validator ACCEPTS is one the browser actually applies (#868)", async ({
  page,
}) => {
  // The defect this pins, found in review: `123, monospace` passed both validators and was
  // persisted as the operator's choice — but an identifier cannot start with a digit, so
  // Chromium discards the WHOLE declaration and keeps the previous face. Settings said one
  // thing, the terminal rendered another, and nothing surfaced the disagreement.
  //
  // Asserted against the browser's real parser rather than a second regex, because a second
  // regex would only prove our two regexes agree with each other.
  await page.goto(settingsPath("appearance"));
  const rejected = await page.evaluate((stacks: string[]) => {
    const el = document.createElement("div");
    return stacks.filter((v) => {
      el.style.fontFamily = "";
      el.style.fontFamily = v;
      return el.style.fontFamily === "";
    });
  }, FIXTURE.write_accepted);
  expect(rejected, "the validator accepted a stack the browser refuses").toEqual([]);
});

test("the stacks the validator REFUSES on grammar, the browser refuses too (#868)", async ({
  page,
}) => {
  // The other direction of the same contract — these are the rows where both must say no, so
  // the grammar is pinned to CSS rather than to our own opinion. (Our validator is stricter in
  // a few places on purpose, e.g. an unbalanced quote, which Chromium auto-closes; those rows
  // deliberately are NOT in this list.)
  await page.goto(settingsPath("appearance"));
  const accepted = await page.evaluate((stacks: string[]) => {
    const el = document.createElement("div");
    return stacks.filter((v) => {
      el.style.fontFamily = "";
      el.style.fontFamily = v;
      return el.style.fontFamily !== "";
    });
  }, FIXTURE.browser_rejects);
  expect(accepted, "the browser accepts a stack we call invalid on grammar").toEqual([]);
});

test("a digit-leading stack is refused, and the live face does not change (#868)", async ({
  page,
}) => {
  // End to end, through the shipped control: the exact value from the review.
  await chooseCustomFace(page, "sans-serif");
  await page.goto(settingsPath("appearance"));
  const field = page.getByLabel("Custom font stack");
  await field.fill("123, monospace");
  await field.press("Enter");
  await expect(page.getByRole("alert")).toBeVisible();

  await page.goto("/s/claude/font-digit-leading");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect.poll(() => terminalFace(page)).toBe("sans-serif");
});

test("a custom face seeded LATE by /api/config is shown as selected and editable (#868)", async ({
  page,
}) => {
  // <ConfigProvider> renders children before /api/config resolves, so Settings mounts on the
  // default System stack and the server's value arrives afterwards. Before the fix, the panel
  // kept describing the initial value: every radio unchecked, the Custom field closed, and the
  // face that was actually live neither visible nor editable.
  //
  // The delay is the whole point of the test, so it is explicit rather than incidental.
  await page.route("**/api/config", async (r) => {
    await new Promise((res) => setTimeout(res, 400));
    await r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        term_font_size: 13,
        term_font_family: "sans-serif",
      },
    });
  });

  await page.goto(settingsPath("appearance"));
  const custom = page.getByRole("radio", { name: /^Custom/ });
  await expect(custom).toHaveAttribute("aria-checked", "true");
  await expect(page.getByLabel("Custom font stack")).toHaveValue("sans-serif");
  // No preset may claim the selection at the same time.
  await expect(page.getByRole("radio", { name: /^System/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
});

test("a LATE seed does not delete what the operator is typing (#868)", async ({
  page,
}) => {
  // The other half of the reconciliation contract: reconcile on an external change, but never
  // over an in-progress draft. Gated like its sibling — this test also depends on the edit
  // happening BEFORE the seed, so a sleep would let a loaded runner reorder it into a false
  // green.
  const seed = await seedLate(page, "sans-serif");

  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  const field = page.getByLabel("Custom font stack");
  await field.click();
  await field.fill("Menlo, mono");
  await expect(field).toBeFocused();

  await seed.land();

  await expect(field).toHaveValue("Menlo, mono");
});

test("FUZZ: nothing the validator accepts is refused by the browser's own parser (#868)", async ({
  page,
}) => {
  // Two rounds of review found two leaks in this validator, each reported as a single value
  // (`123, monospace`, then `serif foo, monospace`). Patching the reported value is what let the
  // second one exist, so this test is the CLASS rather than the cases: a deterministic corpus
  // built from the tokens that actually distinguish CSS family grammar, run through the real
  // validator (imported, not re-implemented) and the real CSS parser, asserting the contract the
  // module documents — **everything we accept, the browser accepts**.
  //
  // The corpus is seeded and the tokens are fixed, so this is reproducible, not flaky. It is the
  // tool that found the second leak: 170 violations before the leading-generic rule, 0 after.
  const TOKENS = [
    "Menlo", "serif", "monospace", "ui-monospace", "system-ui", "math", "emoji",
    "PT", "Serif", "Mono", "123", "1Password", "-apple-system", "--weird", "-2cool",
    "_priv", "A1", "Font.Name", "inherit", "INHERIT", "default", "revert-layer",
    "Segoe", "UI", "x",
  ];
  const segments: string[] = [];
  for (const t of TOKENS) {
    segments.push(t, `"${t}"`, `'${t}'`, `${t} Mono 2`);
    for (const u of TOKENS) segments.push(`${t} ${u}`);
  }
  segments.push("", "   ", '"unclosed', 'tail"', "a.b", "9lives");

  const candidates = new Set<string>();
  for (const s of segments) {
    candidates.add(s);
    candidates.add(`${s}, monospace`);
  }
  for (let i = 0; i < segments.length - 1; i += 7) {
    candidates.add(`${segments[i]}, ${segments[i + 1]}, monospace`);
  }
  const cases = [...candidates].filter((c) => c.length <= TERM_FONT_FAMILY_MAX_LEN).sort();
  expect(cases.length).toBeGreaterThan(500); // a corpus that shrank is not a corpus

  await page.goto(settingsPath("appearance"));
  const browserAccepts: boolean[] = await page.evaluate((cs: string[]) => {
    const el = document.createElement("div");
    return cs.map((v) => {
      el.style.fontFamily = "";
      el.style.fontFamily = v;
      return el.style.fontFamily !== "";
    });
  }, cases);

  const leaks = cases.filter(
    (c, i) => isTermFontFamily(c) && !browserAccepts[i],
  );
  expect(
    leaks,
    "the validator accepts stacks the browser discards — the UI would claim a face the terminal never applied",
  ).toEqual([]);

  // And the converse is NOT asserted: we are deliberately stricter in places (an unclosed
  // quote, `--weird`, a leading ui-* generic). Recorded as a number so a change in that
  // posture is visible in review rather than silent.
  const stricter = cases.filter((c, i) => !isTermFontFamily(c) && browserAccepts[i]);
  expect(stricter.length).toBeGreaterThan(0);
});

test("a late PRESET seed does not unmount the Custom editor mid-edit (#868)", async ({
  page,
}) => {
  // The branch the first delayed-seed regression could not reach: it seeds a CUSTOM stack,
  // which leaves the editor open either way. A seed carrying a *preset* — the common case, when
  // another device last picked one — took the opposite path and closed the input while the
  // operator was typing in it.
  const seed = await seedLate(page, FIRA_PRESET);

  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  const field = page.getByLabel("Custom font stack");
  await field.click();
  await field.fill("Menlo, mono");
  await expect(field).toBeFocused(); // the edit is real BEFORE the seed is released

  await seed.land();

  // Mounted, intact and still the focused element — "the text came back when I reopened it" is
  // not the same claim.
  await expect(field).toBeVisible();
  await expect(field).toHaveValue("Menlo, mono");
  await expect(field).toBeFocused();
});

test("cancelling a dirty draft after a late preset seed re-syncs the picker (#868)", async ({
  page,
}) => {
  // The cost of the fix above, found in the next review round. While the draft is dirty the
  // reconciliation deliberately does nothing — so the pending family must NOT be marked
  // consumed, or it is lost forever: empty the field, let the seed land, blur to cancel, and
  // the terminal runs Fira Code while the picker still claims Custom with an empty editor.
  const seed = await seedLate(page, FIRA_PRESET);

  await page.goto(settingsPath("appearance"));
  await page.getByRole("radio", { name: /^Custom/ }).click();
  const field = page.getByLabel("Custom font stack");
  await field.click();
  await field.fill(""); // emptied — "never mind", the cancel path
  await expect(field).toBeFocused();

  await seed.land();

  // Blur to cancel. The family never changes here; what must change is the PICKER.
  await page.getByRole("heading", { name: "Appearance" }).click();

  await expect(
    page.getByRole("radio", { name: /^Fira Code/ }),
    "the seeded preset must be the one shown as selected",
  ).toHaveAttribute("aria-checked", "true");
  await expect(
    page.getByLabel("Custom font stack"),
    "the empty custom editor must not survive the cancel",
  ).toHaveCount(0);
});
