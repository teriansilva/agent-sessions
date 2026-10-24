/** The New session page in NIGHTJAR (#1531 Phase 6), in a real browser — what jsdom cannot show:
 *
 *  - every control on the form is a 44px target (design §8): the tiles, the project rows, the
 *    model select, the skip checkbox's row, + New project…, the summary's link and Start;
 *  - nothing scrolls sideways at 320, 390, 800 or 1280 px;
 *  - the focus ring is the inset `--accent-text` ring (2px, offset -3px) on a tile, a project row,
 *    the select and the checkbox row;
 *  - the chrome is the v2 primitives: a display-face heading, Start as the primary plate (its
 *    chamfer is paint behind a full-rectangle hit box, never a clip on the button itself), the
 *    agent tile a plate too, the project list a clipped frame;
 *  - motion follows the mode: choosing a tile locks on (one brightness step) in Full, and nothing
 *    moves in Off.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { NEW_SESSION_PATH } from "../src/lib/routes";
import { agentPicker, projectPicker } from "./newSessionForm";
import { storeMotionChoice } from "./nightjarShell";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

const PROJECTS = [
  {
    id: "p-1",
    name: "agent-sessions",
    color: "#ffb000",
    folders: ["/home/u/claude/agent-sessions"],
    default_folder: "/home/u/claude/agent-sessions",
    archived: false,
    created_at: 0,
  },
  {
    id: "p-2",
    name: "scratch",
    color: "#3b9eff",
    folders: [],
    default_folder: "",
    archived: false,
    created_at: 0,
  },
];

async function mockBackend(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude", "codex", "opencode"],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project_id: "p-1",
        agent_defaults: { default_engine: "claude", bypass: false },
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: PROJECTS } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await mockRoster(page);
}

async function open(page: Page, motion: "full" | "off" = "off") {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await storeMotionChoice(page, motion);
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  await expect(page.getByRole("button", { name: /start session/i })).toBeEnabled();
}

const height = async (l: Locator) => (await l.boundingBox())!.height;

/** The inset focus ring (design §6 / §8), read off the element that draws it. */
async function ring(l: Locator) {
  return l.evaluate((el) => {
    const s = getComputedStyle(el);
    const probe = document.createElement("i");
    probe.style.color = "var(--accent-text)";
    document.body.append(probe);
    const accentText = getComputedStyle(probe).color;
    probe.remove();
    return {
      style: s.outlineStyle,
      width: s.outlineWidth,
      offset: s.outlineOffset,
      isAccentText: s.outlineColor === accentText,
    };
  });
}
const INSET = { style: "solid", width: "2px", offset: "-3px", isAccentText: true };

test("every control on the form is a 44px target (design §8)", async ({ page }) => {
  await open(page);
  const targets: [string, Locator][] = [
    ["agent tile", page.locator('label:has(> input[name="new-session-agent"])').first()],
    ["project row", page.locator('label:has(> input[name="new-session-project"])').first()],
    ["model select", page.getByTestId("new-session-model")],
    ["skip permission row", page.locator("label", { has: page.getByRole("checkbox", { name: /skip permission/i }) })],
    ["+ New project…", page.getByRole("link", { name: /new project/i })],
    ["Start", page.getByRole("button", { name: /start session/i })],
  ];
  for (const [what, l] of targets) {
    await l.scrollIntoViewIfNeeded();
    expect(await height(l), what).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  // The folderless project's way out is a real link, at the floor too (#1458).
  await projectPicker(page).selectOption("p-2");
  const link = page.getByTestId("new-session-summary").getByRole("link", { name: "Settings → Projects" });
  expect(await height(link)).toBeGreaterThanOrEqual(TOUCH_FLOOR);
});

for (const width of [320, 390, 800, 1280]) {
  test(`nothing scrolls sideways at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    const overflow = await page.evaluate(() => {
      const doc = document.scrollingElement!;
      const scroller = document.querySelector("[data-new-session-form] > div") as HTMLElement;
      const bar = document.querySelector('[data-testid="new-session-summary"]')!.parentElement!;
      return [doc, scroller, bar].map((el) => el.scrollWidth - el.clientWidth);
    });
    expect(overflow).toEqual([0, 0, 0]);
  });
}

test("the focus ring is drawn inset, in --accent-text (design §6)", async ({ page }) => {
  await open(page);
  const agent = agentPicker(page);
  await agent.radio("claude").focus();
  await page.keyboard.press("ArrowRight"); // keyboard focus: :focus-visible
  await expect(agent.radio("codex")).toBeFocused();
  expect(await ring(page.locator('label:has(> input[value="codex"])'))).toEqual(INSET);

  const project = projectPicker(page);
  await project.radio("p-1").focus();
  await page.keyboard.press("ArrowUp");
  await expect(project.radio("")).toBeFocused();
  expect(await ring(page.locator('label:has(> input[name="new-session-project"][value=""])'))).toEqual(INSET);

  const select = page.getByTestId("new-session-model");
  await select.focus();
  expect(await ring(select)).toEqual(INSET);

  const skip = page.getByRole("checkbox", { name: /skip permission/i });
  await skip.focus();
  await page.keyboard.press("Space");
  expect(await ring(page.locator("label", { has: skip }))).toEqual(INSET);
});

test("the chrome is the v2 primitives: display heading, plates, a clipped list frame", async ({ page }) => {
  await open(page);
  const look = await page.evaluate(() => {
    const heading = document.querySelector("h1")!;
    const start = [...document.querySelectorAll("button")].find((b) => /start session/i.test(b.textContent ?? ""))!;
    const tile = document.querySelector('label:has(> input[name="new-session-agent"])')!;
    const list = document.querySelector('[role="radiogroup"][aria-label="Project"]')!.parentElement!;
    const before = (el: Element) => getComputedStyle(el, "::before").clipPath;
    return {
      headingFont: getComputedStyle(heading).fontFamily,
      headingCaps: getComputedStyle(heading).textTransform,
      startClip: getComputedStyle(start).clipPath,
      startPlate: before(start),
      tileClip: getComputedStyle(tile).clipPath,
      tilePlate: before(tile),
      listFrame: getComputedStyle(list).clipPath,
      radii: [heading, start, tile, list].map((el) => getComputedStyle(el).borderRadius),
    };
  });
  expect(look.headingFont).toContain("BattleLab Display");
  expect(look.headingCaps).toBe("uppercase");
  // Controls PAINT their chamfer (the hit box stays a full rectangle, #782); panels clip.
  expect(look.startClip).toBe("none");
  expect(look.startPlate).toMatch(/^polygon/);
  expect(look.tileClip).toBe("none");
  expect(look.tilePlate).toMatch(/^polygon/);
  expect(look.listFrame).toMatch(/^polygon/);
  expect(look.radii.every((r) => r === "0px")).toBe(true);
});

test("choosing a tile locks on in Full (design §8)", async ({ page }) => {
  await open(page, "full");
  await agentPicker(page).selectOption("codex");
  const tile = page.locator('label:has(> input[value="codex"])');
  const names = await tile.evaluate((el) => el.getAnimations().map((a) => (a as CSSAnimation).animationName));
  expect(names).toContain("nj-glitch-step");
});

test("in Off, choosing a tile runs no animation", async ({ page }) => {
  await open(page, "off");
  await agentPicker(page).selectOption("codex");
  const tile = page.locator('label:has(> input[value="codex"])');
  expect(await tile.evaluate((el) => el.getAnimations().length)).toBe(0);
});
