/** One dialog design, measured in a real browser (#948).
 *
 * The report: "Adopt to mission" rendered with a purple panel and purple option rows, and "Move to
 * project" had the same problem. Both drew on a stylesheet written against tokens the app never
 * defined (`--surface-1`, `--mono`, `--focus`), so every hard-coded fallback behind them painted
 * instead: a purple from a retired palette, in both themes. Rename project carried a copy of the same
 * sheet. The operator asked for all of them to look like Hand off (#597).
 *
 * So the reference is the Hand off dialog itself, and each other dialog is compared against what
 * Hand off COMPUTES in the same page and theme, not against copied values. A token swap then moves
 * all four together, and a dialog that drifts from Hand off goes red here.
 *
 * Geometry is compared as well as colour. Adopt and Move render inside the sidebar row, and the
 * sidebar is a containing block for `position: fixed` (a `backdrop-filter` on desktop, a `transform`
 * on the phone drawer). That trapped the backdrop in a 320px column instead of the viewport. A
 * colour-only check would pass that layout.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { missionRow } from "./mission-console";
import { setupBench } from "./terminal/harness";

const ENGINE = "claude";
const UUID = "cccccccc-1111-2222-3333-444444444444";
const KEY = `${ENGINE}:${UUID}`;
const TITLE = "Tidy the upload retry path";
/** The retired panel colour the undefined `--surface-1` fell back to. */
const OLD_PURPLE = "rgb(27, 18, 38)";

async function stub(page: Page, theme: "dark" | "light") {
  // The DEVICE choice is seeded before first paint. The device cache wins over `/api/config`, so
  // setting it after load would race the reconcile.
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  // Registered after the bench, so these win (Playwright matches the newest route first).
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: KEY,
            engine: ENGINE,
            uuid: UUID,
            short_uuid: UUID.slice(0, 8),
            cwd: "/home/u/proj",
            project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
            last_mtime: 1_700_000_000,
            first_user_message: "",
            title: TITLE,
            sticky: false,
            archived: false,
            // A known, empty membership is what makes the row offer "Adopt to mission".
            mission: null,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: 1 },
      },
    }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({
      json: {
        engines: [
          { id: "claude", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/claude" },
          { id: "codex", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/codex" },
        ],
      },
    }),
  );
  await page.route("**/api/handoff/prepare", (r) =>
    r.fulfill({
      json: { handle: "h-parity", preview: "# Handoff", meta: { mode: "quick", turns: 1, bytes: 9, cap: 8192 } },
    }),
  );
  await page.route(/\/api\/missions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        missions: [
          missionRow({ id: "msn_" + "a".repeat(32), title: "Harden the upload retry path", state: "running" }),
          missionRow({ id: "msn_" + "d".repeat(32), title: "Theme the login page", state: "done" }),
        ],
        total: 2,
        limit: 50,
        offset: 0,
        facets: { projects: [], states: [] },
        store_error: null,
        snapshot: "s",
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p-1", name: "Uploads", color: "#5fd7ff", folders: [], archived: false, created_at: 0, session_count: 1 },
        ],
      },
    }),
  );
}

/** On a phone the list is the off-canvas drawer. It is opened once and survives each dialog's
 *  Escape, which closes only the dialog on top of it (#940). */
async function openRowMenu(page: Page) {
  const toggle = page.getByRole("button", { name: /Open session list/i });
  if ((await toggle.count()) && !(await page.locator('aside.sidebar[role="dialog"]').count())) {
    await toggle.first().click();
    await page.waitForFunction(() => {
      const el = document.querySelector("aside.sidebar");
      return el !== null && el.getBoundingClientRect().x >= 0;
    });
  }
  await page.getByRole("button", { name: "Session actions" }).first().click();
}

/** What a dialog looks like, as the browser computed it. Parts are located by role and label, never by
 *  class name, so this works for all four dialogs without knowing their CSS modules. */
async function look(dialog: Locator) {
  await expect(dialog).toBeVisible();
  return dialog.evaluate((el) => {
    const css = (n: Element | null | undefined, keys: string[]) => {
      if (!n) return null;
      const s = getComputedStyle(n);
      return Object.fromEntries(keys.map((k) => [k, s.getPropertyValue(k)]));
    };
    const tag = document.getElementById(el.getAttribute("aria-labelledby") ?? "");
    const close = el.querySelector('button[aria-label^="Close"]');
    const cancel = [...el.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Cancel");
    const r = el.getBoundingClientRect();
    return {
      panel: css(el, [
        "background-color",
        "color",
        "border-top-color",
        "border-top-width",
        "border-top-style",
        "border-radius",
        "padding-top",
        "padding-left",
        "box-shadow",
      ]),
      backdrop: css(el.parentElement, ["background-color", "align-items", "justify-content"]),
      tag: css(tag, ["font-family", "font-size", "font-weight", "letter-spacing", "text-transform", "color"]),
      close: css(close, ["width", "height", "border-top-color", "background-color", "color"]),
      cancel: css(cancel, ["min-height", "font-size", "color", "background-color", "border-top-color"]),
      box: {
        left: Math.round(r.left),
        right: Math.round(innerWidth - r.right),
        bottom: Math.round(innerHeight - r.bottom),
        centreX: Math.round(r.left + r.width / 2 - innerWidth / 2),
      },
    };
  });
}

type Look = Awaited<ReturnType<typeof look>>;

/** A row's colour and border as the browser computed it, with no pointer over it. Layout is left out on
 *  purpose: a mission row has two lines and a project row one. */
async function rowLook(row: Locator) {
  await row.page().mouse.move(0, 0);
  return row.evaluate((el) => {
    const s = getComputedStyle(el);
    return Object.fromEntries(
      ["background-color", "border-top-color", "border-top-width", "border-top-style", "color", "opacity"].map(
        (k) => [k, s.getPropertyValue(k)],
      ),
    );
  });
}

/** WCAG contrast of the row's metadata line against the surface it actually sits on (#961 review 4813). */
async function metaContrast(row: Locator) {
  return row.evaluate((el) => {
    const meta = [...el.querySelectorAll("span")].filter((n) => /sessions? ·/.test(n.textContent ?? "")).pop();
    if (!meta) return 0;
    const rgb = (c: string) => (c.match(/[\d.]+/g) ?? []).map(Number);
    const lum = ([r, g, b]: number[]) => {
      const lin = (v: number) => ((v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
      return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    };
    let bgNode: Element | null = meta;
    let bg = [0, 0, 0];
    while (bgNode) {
      const c = rgb(getComputedStyle(bgNode).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0)) {
        bg = c.slice(0, 3);
        break;
      }
      bgNode = bgNode.parentElement;
    }
    const [a, b] = [lum(rgb(getComputedStyle(meta).color).slice(0, 3)), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

function expectSameDesign(name: string, got: Look, ref: Look, mobile: boolean) {
  expect.soft(got.panel?.["background-color"], `${name}: not the retired purple`).not.toBe(OLD_PURPLE);
  expect.soft(got.panel, `${name}: panel`).toEqual(ref.panel);
  expect.soft(got.backdrop, `${name}: backdrop`).toEqual(ref.backdrop);
  expect.soft(got.tag, `${name}: head tag`).toEqual(ref.tag);
  expect.soft(got.close, `${name}: close button`).toEqual(ref.close);
  expect.soft(got.cancel, `${name}: cancel button`).toEqual(ref.cancel);
  if (mobile) {
    // The bottom sheet: the same inset from both sides and the floor of the viewport.
    expect.soft(
      { left: got.box.left, right: got.box.right, bottom: got.box.bottom },
      `${name}: bottom sheet`,
    ).toEqual({ left: ref.box.left, right: ref.box.right, bottom: ref.box.bottom });
  } else {
    // Centred on the viewport, not on whatever column the dialog was rendered from.
    expect.soft(Math.abs(got.box.centreX), `${name}: centred on the viewport`).toBeLessThanOrEqual(1);
  }
}

for (const theme of ["dark", "light"] as const) {
  test(`adopt, move and rename share the Hand off dialog design in the ${theme} theme (#948)`, async ({
    page,
  }, testInfo) => {
    test.setTimeout(90_000);
    const mobile = testInfo.project.name === "mobile";
    await stub(page, theme);
    await page.goto("/");
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

    await openRowMenu(page);
    await page.getByRole("menuitem", { name: "Hand off session to another engine" }).click();
    const handoff = page.getByRole("dialog", { name: /hand off/i });
    const ref = await look(handoff);
    // The reference is itself on the real tokens. Without this, the comparison could pass with all
    // four dialogs purple.
    expect(ref.panel?.["background-color"]).not.toBe(OLD_PURPLE);
    expect(ref.tag?.["text-transform"]).toBe("uppercase");
    if (!mobile) expect(Math.abs(ref.box.centreX)).toBeLessThanOrEqual(1);
    await page.keyboard.press("Escape");
    await expect(handoff).toBeHidden();

    await openRowMenu(page);
    await page.getByRole("menuitem", { name: "Adopt session to a mission" }).click();
    const adopt = page.getByRole("dialog", { name: "Adopt to mission" });
    await expect(adopt.getByTestId("adopt-option")).toHaveCount(2);
    expectSameDesign("Adopt to mission", await look(adopt), ref, mobile);

    // ROWS, not only chrome (#961 review 4813): a row-only regression goes red too. An enabled row's
    // metadata is information the operator reads, so it must clear small-text AA on its own row.
    const enabledRow = adopt.getByTestId("adopt-option").filter({ hasText: "Harden the upload retry path" });
    const refusedRow = adopt.getByTestId("adopt-option").filter({ hasText: "Theme the login page" });
    const adoptNormal = await rowLook(enabledRow);
    expect.soft(await metaContrast(enabledRow), "Adopt: enabled row metadata contrast").toBeGreaterThanOrEqual(4.5);
    const refused = await rowLook(refusedRow);
    expect.soft(refused["border-top-style"], "Adopt: a refused mission is dashed").toBe("dashed");
    expect.soft(Number(refused.opacity), "Adopt: a refused mission is dimmed").toBeLessThan(1);
    await enabledRow.click();
    await expect(enabledRow).toHaveAttribute("aria-pressed", "true");
    const adoptSelected = await rowLook(enabledRow);
    expect.soft(adoptSelected, "Adopt: selecting a row changes it").not.toEqual(adoptNormal);
    await page.keyboard.press("Escape");
    await expect(adopt).toBeHidden();

    await openRowMenu(page);
    await page.getByRole("menuitem", { name: "Move session to a project" }).click();
    const move = page.getByRole("dialog", { name: "Move to project" });
    await expect(move.getByRole("button", { name: /Uploads/ })).toBeVisible();
    expectSameDesign("Move to project", await look(move), ref, mobile);
    // The same row language in both pickers: a plain row, and the chosen/current one.
    expect.soft(await rowLook(move.getByRole("button", { name: /Uploads/ })), "rows: normal").toEqual(adoptNormal);
    expect.soft(await rowLook(move.getByRole("button", { name: /Default project/ })), "rows: current").toEqual(adoptSelected);
    await page.keyboard.press("Escape");
    await expect(move).toBeHidden();

    await page.goto("/settings/projects");
    await page.getByRole("button", { name: "Rename ~/proj" }).click();
    const rename = page.getByRole("dialog", { name: "Rename project" });
    expectSameDesign("Rename project", await look(rename), ref, mobile);
  });
}
