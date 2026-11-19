/** Mission Control polish, P1 (#967): one composer box, one header action row, one button system,
 *  one Send — measured in a real browser.
 *
 *  Every comparison here is against the thing the design says it IS, computed in the same browser:
 *  the landing, header and plan-card buttons against the Hand off dialog's own `.go` / `.cancel`,
 *  and the mission Send against the session pane's Send. Copied values in a test would pass the day
 *  the two drift; a live reference fails it.
 *
 *  Geometry is compared by boxes, never by counting rows: "one row" is a claim about where the
 *  controls sit relative to each other, and a wrapped control can still leave a row count of one.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";
import { setupBench } from "./terminal/harness";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { animationsSettled } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const T = 1_700_000_000;
const ENGINE = "claude";
const UUID = "dddddddd-1111-2222-3333-444444444444";
const KEY = `${ENGINE}:${UUID}`;

const PLAN = {
  plan_id: "pln_1",
  mission_id: "msn_1",
  project_id: "p1",
  cwd: "/repo/alpha",
  engine: "claude",
  engine_reason: "it is a python repo",
  brief: "Fix the upload retry and open a PR",
  created_at: T,
  project_options: [{ id: "p1", name: "Alpha", cwd: "/repo/alpha" }],
  engine_options: [{ id: "claude", label: "Claude" }],
};
const OBJECTIVES = [{ key: "pr", title: "A PR is open", gate: true }];

/** The four header states the issue names, each as the detail read the console renders. */
const STATES: Record<string, Record<string, unknown>> = {
  draft: { state: "draft", plan: null, objectives: [], objectives_state: "done" },
  planned: { state: "planned", plan: PLAN, objectives: OBJECTIVES, objectives_state: "done" },
  running: { state: "running" },
  failed: { state: "failed", outcome: "failed", closed_at: T },
};

const TITLES: Record<string, string> = {
  draft: "Draft mission",
  planned: "Planned mission",
  running: "Running mission",
  failed: "Failed mission",
};

/** Everything `/mission` reads, with one mission per named state. */
async function stubMissions(page: Page, only?: string[]) {
  const names = only ?? Object.keys(STATES);
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "Alpha", folders: ["/repo/alpha"] }] } }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [] } }));
  await mockMissions(page, {
    missions: missionList(
      names.map((n) =>
        missionRow({
          id: `msn_${n}`,
          title: TITLES[n],
          project_id: "p1",
          state: STATES[n].state,
        }),
      ),
    ),
  });
  await page.route(/\/api\/missions\/msn_[a-z]+(\?.*)?$/, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop()!;
    const n = id.slice(4);
    return r.fulfill({
      json: {
        ...MISSION,
        id,
        title: TITLES[n],
        project_id: "p1",
        events: [],
        events_next_seq: null,
        ...STATES[n],
      },
    });
  });
}

async function selectMission(page: Page, title: string) {
  await openMissionRail(page);
  await page.getByTestId("rail-mission").filter({ hasText: title }).first().click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("console-title")).toHaveText(title);
}

/** The properties that make two buttons the same button, as the browser computed them. Read with no
 *  pointer over the control and nothing focused, so `:hover` and `:focus-visible` cannot differ. */
const BUTTON_KEYS = [
  "min-height",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "font-family",
  "font-size",
  "font-weight",
  "letter-spacing",
  "text-transform",
  "color",
  "background-color",
  "border-top-width",
  "border-top-style",
  "border-top-color",
  "border-radius",
  "opacity",
];

async function buttonLook(el: Locator, keys = BUTTON_KEYS) {
  await el.page().mouse.move(0, 0);
  await expect(el).toBeVisible();
  // At rest: a plate's face eases out of its hover colour over --t-fast after the pointer leaves.
  await animationsSettled(el.page());
  return el.evaluate((node, keys) => {
    (document.activeElement as HTMLElement | null)?.blur?.();
    const s = getComputedStyle(node);
    // A plate (#1531) paints its face and edge on ::after / ::before behind its text; its own
    // background is transparent on purpose. Its look is read from the painted layers.
    const face = getComputedStyle(node, "::after");
    const edge = getComputedStyle(node, "::before");
    const plate = face.content !== "none" && face.content !== "normal";
    return Object.fromEntries(
      keys.map((k) => [
        k,
        plate && k === "background-color"
          ? face.backgroundColor
          : plate && k.startsWith("border") && k.endsWith("color")
            ? edge.backgroundImage
            : s.getPropertyValue(k),
      ]),
    );
  }, keys);
}

async function box(el: Locator) {
  const b = await el.boundingBox();
  expect(b, "element has a box").not.toBeNull();
  return b!;
}

// ---------------------------------------------------------------------------------------------------
// The start screen
// ---------------------------------------------------------------------------------------------------

// RETIRED BY #2152 (no New-mission form): "the start screen is ONE composer box: the brief on
// top, one footer row inside it, no Cancel (#967)" measured that form's box, footer and wrap. What
// the start screen is now:
test("the start screen is the landing: New mission is its one primary, at the touch floor, with no form (#2152)", async ({
  page,
}, info) => {
  await stubMissions(page);
  await page.goto("/mission");
  const landing = page.getByTestId("mission-landing");
  await expect(landing).toBeVisible();
  const button = page.getByTestId("landing-new-mission");
  await expect(button).toHaveText("New mission");
  expect((await box(button)).height, "44px floor").toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // One primary, and nothing to type into or cancel.
  await expect(landing.locator("textarea, select")).toHaveCount(0);
  await expect(page.getByTestId("new-mission-cancel")).toHaveCount(0);
  // Inside the page, and below the heading that explains it.
  const hb = await box(page.getByTestId("mission-landing-heading"));
  const bb = await box(button);
  expect(bb.y).toBeGreaterThan(hb.y);
  expect(bb.x + bb.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await page.screenshot({ path: `test-results/p1-start-screen-${info.project.name}.png` });
});

// ---------------------------------------------------------------------------------------------------
// The header
// ---------------------------------------------------------------------------------------------------

/** The row's one action, by state. A draft with no plan and no session has NONE (#2152): Begin is
 *  a legacy plan's or an attached session's, and the plan card offers Plan with HQ instead. */
const PRIMARY: Record<string, string | null> = {
  draft: null,
  planned: "mission-begin",
  running: "mission-done",
  failed: "mission-reopen",
};

for (const name of Object.keys(STATES)) {
  test(`the ${name} header is one centred row — a state chip, at most one primary, and ⋯ (#967)`, async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await stubMissions(page, [name]);
    for (const width of [801, 1280, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/mission");
      await selectMission(page, TITLES[name]);

      const actions = page.getByTestId("header-actions");
      const title = page.getByRole("button", { name: "Show full mission title" });
      const chip = page.getByTestId("mission-state-chip");
      const dot = chip.getByTestId("mission-state-dot");
      const primaryId = PRIMARY[name];
      const more = page.getByTestId("mission-overflow");

      // THE CHIP: a 6px status dot beside a mono label.
      await expect(chip).toBeVisible();
      const db = await box(dot);
      expect([Math.round(db.width), Math.round(db.height)], `dot at ${width}`).toEqual([6, 6]);
      await expect(page.getByTestId("mission-state")).toHaveText(name);

      // AT MOST ONE ACTION BUTTON beside ⋯, and no sentence in the row: the reason lives in the plan card.
      const buttons = actions.locator("button:visible");
      const ids = await buttons.evaluateAll((els) => els.map((e) => e.getAttribute("data-testid")));
      expect(ids.filter((id) => id !== "mission-overflow"), `buttons at ${width}`).toEqual(
        primaryId ? [primaryId] : [],
      );
      await expect(actions.locator('p:visible, [role="status"]:visible')).toHaveCount(0);

      // ONE ROW, vertically centred: the title, the chip, the primary and ⋯ share a centre line,
      // and the 44px controls share the title button's top and bottom edges.
      const tb = await box(title);
      const cb = await box(chip);
      const pb = primaryId ? await box(page.getByTestId(primaryId)) : null;
      const mb = await box(more);
      const mid = (b: { y: number; height: number }) => b.y + b.height / 2;
      const controls = pb
        ? ([["primary", pb], ["⋯", mb]] as const)
        : ([["⋯", mb]] as const);
      for (const [label, b] of [["chip", cb], ...controls] as const) {
        expect(Math.abs(mid(b) - mid(tb)), `${label} centre at ${width}`).toBeLessThan(1.5);
      }
      for (const [label, b] of controls) {
        expect(Math.abs(b.y - tb.y), `${label} top at ${width}`).toBeLessThan(1.5);
        expect(Math.abs(b.y + b.height - (tb.y + tb.height)), `${label} bottom at ${width}`).toBeLessThan(1.5);
      }
      expect(cb.y).toBeGreaterThanOrEqual(tb.y);
      expect(cb.y + cb.height).toBeLessThanOrEqual(tb.y + tb.height);
      // …left to right, and on screen.
      expect(tb.x + tb.width).toBeLessThanOrEqual(cb.x + 0.5);
      if (pb) {
        expect(cb.x + cb.width).toBeLessThanOrEqual(pb.x + 0.5);
        expect(pb.x + pb.width).toBeLessThanOrEqual(mb.x + 0.5);
      } else {
        expect(cb.x + cb.width).toBeLessThanOrEqual(mb.x + 0.5);
      }
      expect(mb.x + mb.width).toBeLessThanOrEqual(width);
      expect([Math.round(mb.width), Math.round(mb.height)], `⋯ at ${width}`).toEqual([44, 44]);
    }

    if (name === "draft") {
      // No Begin, and nothing in the row explains its absence: the PLAN CARD says the mission has
      // no plan (#2152).
      await expect(page.getByTestId("mission-begin")).toHaveCount(0);
      const lead = page.getByTestId("mission-plan-lead");
      await expect(lead).toHaveText("This mission has no plan");
      expect(await lead.evaluate((el) => !!el.closest('[data-testid="mission-plan-card"]'))).toBe(true);
    }
    if (name === "planned") {
      // A legacy plan keeps Review plan behind ⋯; Plan again is gone with the planner (#2152).
      await page.getByTestId("mission-overflow").click();
      await expect(page.getByTestId("mission-review-plan")).toHaveText("Review plan");
      await expect(page.getByTestId("mission-replan")).toHaveCount(0);
    }
  });
}

test("the header ⋯ is a context menu — RowMenu's rows, not a stack of bordered buttons (#967)", async ({
  page,
}) => {
  await stubMissions(page, ["planned"]);
  await page.goto("/mission");
  await selectMission(page, TITLES.planned);
  await page.getByTestId("mission-overflow").click();
  await expect(page.getByTestId("mission-overflow-menu").getByRole("menu")).toBeVisible();
  await page.mouse.move(0, 0);

  const dangerText = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--danger-text)";
    document.body.append(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  });
  const row = (id: string) =>
    page.getByTestId(id).evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        border: [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth, s.borderLeftWidth],
        height: el.getBoundingClientRect().height,
        color: s.color,
        icon: el.querySelector("svg") !== null,
      };
    });

  // A row, not a button: no edge of its own, the 44px floor, an icon beside the label.
  // (Plan again was the row measured here until #2152 retired it; Review plan is the plan group's.)
  const review = await row("mission-review-plan");
  expect(review.border).toEqual(["0px", "0px", "0px", "0px"]);
  expect(review.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(review.icon).toBe(true);
  // A destructive row says so in danger TEXT, never with a red border.
  const abandon = await row("mission-abandon");
  expect(abandon.border).toEqual(["0px", "0px", "0px", "0px"]);
  expect(abandon.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(abandon.color).toBe(dangerText);
  // …and a hairline separates it from the plan group, as RowMenu draws one.
  expect(
    await page.evaluate(() => {
      const menu = document.querySelector('[data-testid="mission-overflow-menu"]')!;
      const sep = menu.querySelector('[role="separator"]');
      const a = menu.querySelector('[data-testid="mission-review-plan"]')!;
      const b = menu.querySelector('[data-testid="mission-abandon"]')!;
      return (
        sep !== null &&
        (a.compareDocumentPosition(sep) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0 &&
        (sep.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
      );
    }),
  ).toBe(true);
});

test("arming Begin from the header brings the launch warning and its Cancel into view, from the bottom of a long thread (#967)", async ({
  page,
}, info) => {
  // Hermes on #976: the header is fixed and the plan card scrolls with the thread, so arming Begin
  // with the thread scrolled to its end left the warning (agent, directory, unattended) and its Cancel
  // thousands of pixels above the visible area, and the second click could dispatch unseen.
  test.setTimeout(90_000);
  await stubMissions(page, ["planned"]);
  const events = Array.from({ length: 40 }, (_, i) => ({
    seq: i + 1,
    kind: "operator_msg",
    text: `Thread entry ${i + 1}: ${"Keep this conversation long enough to scroll. ".repeat(8)}`,
    ts: T + i,
  }));
  await page.route(/\/api\/missions\/msn_planned(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        id: "msn_planned",
        title: TITLES.planned,
        project_id: "p1",
        ...STATES.planned,
        events,
        events_next_seq: null,
      },
    }),
  );
  await page.goto("/mission");
  await selectMission(page, TITLES.planned);
  // The last entry has rendered, so the thread is at its full height before it is scrolled.
  await expect(page.getByText(/^Thread entry 40:/).first()).toBeAttached();

  /** The nearest ancestor that actually scrolls, and whether an element sits inside both it and the
   *  window. Found, not assumed: the scrolling element is not the same at every width. */
  const SCROLLER = `(el) => {
    let n = el.parentElement;
    while (n && !(n.scrollHeight > n.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(n).overflowY)))
      n = n.parentElement;
    return n;
  }`;
  const scroller = await page.getByTestId("mission-plan-card").evaluate((card, src) => {
    const n = (0, eval)(src)(card) as HTMLElement | null;
    if (!n) return null;
    n.scrollTop = n.scrollHeight;
    return `${n.tagName.toLowerCase()}[data-testid=${n.getAttribute("data-testid")}]`;
  }, SCROLLER);
  expect(scroller, "the thread scrolls somewhere").not.toBeNull();
  console.log(`thread scroller [${info.project.name}]: ${scroller}`);
  await expect(page.getByTestId("mission-plan-card")).not.toBeInViewport();

  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toHaveText("Confirm begin");

  const visible = (id: string) =>
    page.getByTestId(id).evaluate((el, src) => {
      const r = el.getBoundingClientRect();
      const n = (0, eval)(src)(el) as HTMLElement | null;
      const box = n ? n.getBoundingClientRect() : { top: 0, bottom: innerHeight };
      return (
        r.height > 0 &&
        r.top >= Math.max(box.top, 0) - 1 &&
        r.bottom <= Math.min(box.bottom, innerHeight) + 1
      );
    }, SCROLLER);
  // (a) The warning and its Cancel are on screen, inside the pane that scrolls and the window.
  await expect.poll(() => visible("mission-dispatch-confirm"), { message: "warning in view" }).toBe(true);
  await expect.poll(() => visible("mission-begin-cancel"), { message: "Cancel in view" }).toBe(true);
  // (b) Focus is inside the smallest element holding both, and on neither control: a repeated Enter
  // must not confirm, and must not silently cancel either.
  expect(
    await page.evaluate(() => {
      const warning = document.querySelector('[data-testid="mission-dispatch-confirm"]')!;
      const cancel = document.querySelector('[data-testid="mission-begin-cancel"]')!;
      let block: Element | null = warning;
      while (block && !block.contains(cancel)) block = block.parentElement;
      const active = document.activeElement;
      return {
        insideBlock: !!block && !!active && block.contains(active),
        onCancel: active === cancel,
        onConfirm: active?.getAttribute("data-testid") === "mission-begin",
      };
    }),
  ).toEqual({ insideBlock: true, onCancel: false, onConfirm: false });
});

// ---------------------------------------------------------------------------------------------------
// One button system
// ---------------------------------------------------------------------------------------------------

async function handOffReference(page: Page) {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: "Tidy the upload retry path" }] });
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
            last_mtime: T,
            first_user_message: "",
            title: "Tidy the upload retry path",
            sticky: false,
            archived: false,
            mission: null,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: 1 },
      },
    }),
  );
  await mockRoster(page, { only: ["claude", "codex"] });
  await page.route("**/api/handoff/prepare", (r) =>
    r.fulfill({
      json: { handle: "h-p1", preview: "# Handoff", meta: { mode: "quick", turns: 1, bytes: 9, cap: 8192 } },
    }),
  );
  await page.goto(NEW_SESSION_PATH);
  const toggle = page.getByRole("button", { name: /Open session list/i });
  if ((await toggle.count()) && !(await page.locator('aside.sidebar[role="dialog"]').count())) {
    await toggle.first().click();
    await page.waitForFunction(() => {
      const el = document.querySelector("aside.sidebar");
      return el !== null && el.getBoundingClientRect().x >= 0;
    });
  }
  await page.getByRole("button", { name: "Session actions" }).first().click();
  await page.getByRole("menuitem", { name: "Hand off session to another engine" }).click();
  const dialog = page.getByRole("dialog", { name: /hand off/i });
  const cancel = dialog.getByRole("button", { name: "Cancel", exact: true });
  const go = cancel.locator("xpath=following-sibling::button[1]");
  await expect(go).toBeEnabled();
  const ref = { go: await buttonLook(go), cancel: await buttonLook(cancel) };
  // The reference is a real CTA and a real ghost, not two empty reads.
  expect(ref.go["background-color"]).not.toBe(ref.cancel["background-color"]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  return ref;
}

test("header, landing and plan-card buttons compute the Hand off dialog's .go / .cancel (#967)", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const ref = await handOffReference(page);

  await stubMissions(page, ["planned", "running"]);
  await page.goto("/mission");

  // THE LANDING: New mission is the CTA (#2152: the form and its Template ghost are gone).
  expect.soft(await buttonLook(page.getByTestId("landing-new-mission")), "landing New mission").toEqual(ref.go);

  // THE HEADER of a planned mission: Begin is the CTA; ⋯ is a ghost icon button.
  await selectMission(page, TITLES.planned);
  const begin = page.getByTestId("mission-begin");
  await expect(begin).toBeEnabled();
  expect.soft(await buttonLook(begin), "header Begin").toEqual(ref.go);
  const ghostColour = ["color", "background-color", "border-top-width", "border-top-style", "border-top-color", "border-radius"];
  const pick = (o: Record<string, string>) => Object.fromEntries(ghostColour.map((k) => [k, o[k]]));
  expect.soft(await buttonLook(page.getByTestId("mission-overflow"), ghostColour), "header ⋯").toEqual(pick(ref.cancel));

  // THE PLAN CARD: arming Begin offers its Cancel there, as a ghost.
  await begin.click();
  await expect(begin).toHaveText("Confirm begin");
  const planCancel = page.getByTestId("mission-plan-card").getByTestId("mission-begin-cancel");
  expect.soft(await buttonLook(planCancel), "plan card Cancel").toEqual(ref.cancel);
  expect.soft(await buttonLook(begin), "header Confirm begin").toEqual(ref.go);

  // …and a running mission's Mark done is the ghost.
  await selectMission(page, TITLES.running);
  expect.soft(await buttonLook(page.getByTestId("mission-done")), "header Mark done").toEqual(ref.cancel);
});

// ---------------------------------------------------------------------------------------------------
// One Send
// ---------------------------------------------------------------------------------------------------

const SEND_KEYS = [
  "height",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "font-family",
  "font-size",
  "font-weight",
  "letter-spacing",
  "text-transform",
  "color",
  "background-color",
  "border-top-width",
  "border-top-style",
  "border-radius",
  "column-gap",
];

async function sendLook(el: Locator) {
  const look = await buttonLook(el, SEND_KEYS);
  const extra = await el.evaluate((node) => {
    const svg = node.querySelector("svg");
    const r = svg?.getBoundingClientRect();
    return { icon: r ? [Math.round(r.width), Math.round(r.height)] : null, label: node.textContent?.trim() };
  });
  return { ...look, ...extra };
}

test("the mission Send IS the session Send — paper plane, label and all (#967)", async ({ page }) => {
  test.setTimeout(90_000);
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: "Tidy the upload retry path" }] });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await expect(page.locator(".xterm")).toBeVisible();
  const opener = page.getByRole("button", { name: "Open compose box" });
  if (await opener.isVisible()) await opener.click();
  const sessionSend = page.locator('button[title="Send + Enter"]');
  const ref = await sendLook(sessionSend);
  expect(ref.icon, "the session Send carries its icon").toEqual([15, 15]);
  expect(ref.label).toBe("Send");

  await stubMissions(page, ["running"]);
  // The thread's Send.
  await page.goto("/mission");
  await selectMission(page, TITLES.running);
  await page.getByTestId("composer-input").fill("status?");
  expect.soft(await sendLook(page.getByTestId("composer-send")), "mission thread Send").toEqual(ref);

  // …and ASK's Send, in the sidebar since #1294 (`/ask` opens it). Same class, same icon, same label — the #967
  // one-Send rule survives the move, which is the point of checking it here at all.
  await page.goto("/ask");
  await page.getByTestId("composer-input").fill("which session was that?");
  expect.soft(await sendLook(page.getByTestId("composer-send")), "ASK page Send").toEqual(ref);
});

test("the launch confirmation says what it will track, and why the set is worth a look (#1061)", async ({
  page,
}) => {
  await stubMissions(page, ["planned"]);
  await page.route(/\/api\/missions\/msn_planned(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...MISSION,
        id: "msn_planned",
        title: TITLES.planned,
        project_id: "p1",
        ...STATES.planned,
        objectives: [
          {
            mission_id: "msn_planned",
            key: "merged",
            ord: 0,
            title: "devopsagent/alpha is merged",
            probe: "forge_merged",
            probe_args: { branch: "devopsagent/alpha" },
            gate: true,
            state: "unmet",
            met_at: null,
            observed: null,
            source: "playbook",
          },
          {
            mission_id: "msn_planned",
            key: "note:1",
            ord: 1,
            title: "Look for file conflicts between the approved PRs before merging",
            probe: "none",
            probe_args: null,
            gate: false,
            state: "unmet",
            met_at: null,
            observed: null,
            source: "model",
          },
        ],
        objectives_fit: { dropped: 1, parameterised: 1 },
        events: [],
        events_next_seq: null,
      },
    }),
  );
  await page.goto("/mission");
  await selectMission(page, TITLES.planned);
  await page.getByTestId("mission-begin").click();
  await expect(page.getByTestId("mission-begin")).toHaveText("Confirm begin");

  const rows = page.getByTestId("mission-confirm-objectives").locator("li");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("Required");
  await expect(rows.nth(0)).toContainText("forge_merged · devopsagent/alpha");
  await expect(rows.nth(1)).toContainText("Goal");
  await expect(rows.nth(1)).toContainText("note · not checked");
  const why = page.getByTestId("mission-confirm-why");
  await expect(why).toContainText("fitted to targets named in your instruction");
  await expect(why).toContainText("did not fit the checklist and was dropped");

  // Attention, not failure: the text-safe amber. And nothing leaves the viewport on a phone.
  const [got, want] = await why.evaluate((el) => {
    const p = document.createElement("span");
    p.style.color = "var(--warn-text)";
    document.body.appendChild(p);
    const w = getComputedStyle(p).color;
    p.remove();
    return [getComputedStyle(el).color, w];
  });
  expect(got).toBe(want);
  const vw = page.viewportSize()!.width;
  for (const i of [0, 1]) {
    const b = await rows.nth(i).boundingBox();
    expect(b!.x + b!.width).toBeLessThanOrEqual(vw + 0.5);
  }

  // Edit objectives disarms the launch: the set it confirmed may change.
  await page.getByTestId("mission-confirm-edit").click();
  await expect(page.getByTestId("mission-begin")).toHaveText("Begin");
  await expect(page.getByTestId("mission-confirm-objectives")).toHaveCount(0);
});
