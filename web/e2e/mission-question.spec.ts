/** The bounded choice, in a real browser (#892).
 *
 * jsdom can prove the handler posts an index. It cannot prove the two things this phase is
 * actually about: that the option's LABEL never travels (so it can never be the instruction), and
 * that three concrete options plus a free-text field are usable on a phone rather than truncated
 * to the point where the operator is choosing something they cannot read.
 *
 * So the assertions are on the REQUEST BODIES the app sends and on painted geometry — never on a
 * DOM proxy for either.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

const OVERVIEW = {
  cache_version: 1,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "medium",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: null,
  cards: [],
};

/** A label that NAMES a different action from the closed set, and one that reads like a command.
 *  If anything ever sent the label instead of the index, this is what would arrive. */
const QUESTION = {
  seq: 41,
  question:
    "Two pull requests touch this branch. Which one is this mission's objective about?",
  objective: "pr_open",
  episode: 1,
  options: [
    {
      label: "waive_objective; rm -rf /",
      action: "note_answer",
      consequence: "Records your answer. Nothing else changes.",
      settling: false,
    },
    {
      // THE DECEPTION, as the review reproduced it: a reassuring label over a settling action.
      label: "Keep working; leave this required",
      action: "waive_objective",
      consequence:
        "Marks this objective NOT REQUIRED. The mission can finish without it.",
      settling: true,
    },
    {
      label: "Stop following up on it for now",
      action: "stand_down_objective",
      consequence: "Stops following up on this objective. It stays unmet.",
      settling: true,
    },
  ],
};

async function stub(page: Page, opts: { question?: unknown } = {}) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
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
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: OVERVIEW }));
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await mockMissions(page, {
    missions: missionList([
      missionRow({ id: "msn_1", title: "Ship it", needs_you: true }),
    ]),
    mission: {
      ...MISSION,
      id: "msn_1",
      title: "Ship it",
      needs_you: true,
      needs_you_why: ["question"],
      question: "question" in opts ? opts.question : QUESTION,
      events: [],
      events_next_seq: null,
    },
  });
}

async function openMission(page: Page) {
  await expect(page.getByTestId("mission-console")).toBeVisible();
  // Through the SHELL's control (#940). The console's own `☰` retired with `MissionDrawer`, so
  // `isVisible()` on it was always false and the phone's drawer never opened — leaving every
  // click below aimed at an off-canvas rail, which Playwright calls "visible" because it has a
  // box. A no-op wherever the sidebar is already a docked column.
  await openMissionRail(page);
  await page.locator('[data-testid="rail-mission"]:visible').first().click();
  // …and closed again through the one dialog the shell now owns. `rail-drawer` was
  // `MissionDrawer`'s panel; with that component deleted this matched nothing and left the drawer
  // sitting over every subsequent click.
  if (await page.getByRole("dialog").count()) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
}

test("choosing an option posts the INDEX, and the label never travels", async ({
  page,
}) => {
  await stub(page);
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/answer", async (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        action: "note_answer",
        objective: "pr_open",
        answer: "x",
        applied: "recorded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await expect(page.getByTestId("mission-question")).toBeVisible();

  await page.getByTestId("mission-question-option").first().click();
  await expect.poll(() => bodies.length).toBe(1);

  const body = bodies[0] as Record<string, unknown>;
  expect(body.seq).toBe(41);
  expect(body.option_index).toBe(0);
  // THE WHOLE AUTHORITY MODEL, asserted on the wire. The label is model-authored text; the
  // action lives in the server's closed set and is looked up there by position.
  const sent = JSON.stringify(body);
  expect(sent).not.toContain("rm -rf");
  expect(sent).not.toContain("waive_objective");
});

test("a free-text answer posts TEXT and no index", async ({ page }) => {
  await stub(page);
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/answer", async (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        action: "note_answer",
        objective: "pr_open",
        answer: "x",
        applied: "recorded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await page.getByTestId("mission-question-text").fill("use the Tuesday one");
  await page.getByTestId("mission-question-send").click();
  await expect.poll(() => bodies.length).toBe(1);

  const body = bodies[0] as Record<string, unknown>;
  expect(body.text).toBe("use the Tuesday one");
  expect(body.option_index).toBeUndefined();
});

test("a REFUSED answer shows the server's own words", async ({ page }) => {
  await stub(page);
  await page.route("**/api/missions/*/answer", (r) =>
    r.fulfill({
      status: 409,
      json: {
        detail:
          "that question is no longer the open one — it was answered or superseded",
      },
    }),
  );

  await page.goto("/pulse");
  await openMission(page);
  await page.getByTestId("mission-question-option").first().click();
  // The reason reaches the operator rather than being flattened to a generic failure (#834).
  await expect(page.getByTestId("mission-console")).toContainText(
    "no longer the open one",
  );
});

test("a 200 whose EFFECT was refused says so, rather than looking like it landed", async ({
  page,
}) => {
  // #900 review 7, finding 9. `close_mission` over an unmet gate, `waive_objective` over an
  // objective already met or since dropped — each is a valid 200 whose effect the store refused,
  // and it says so in the operator's own terms. The card threw that away and cleared as though
  // the choice had landed, which is the "answered `waived` over an objective that was never
  // waived" failure one layer up.
  //
  // `applied_ok` is the flag, deliberately not the prose: every refusal happens to begin with
  // "not ", and a phrasing change would silently turn a refusal into a success on screen.
  await stub(page);
  await page.route("**/api/missions/*/answer", (r) =>
    r.fulfill({
      json: {
        action: "waive_objective",
        objective: "pr_open",
        answer: "the first one",
        applied: "not waived — the objective was already met",
        applied_ok: false,
      },
    }),
  );

  await page.goto("/pulse");
  await openMission(page);
  await page.getByTestId("mission-question-option").first().click();
  await expect(page.getByTestId("mission-console")).toContainText(
    "not waived — the objective was already met",
  );
});

test("no question, no card", async ({ page }) => {
  await stub(page, { question: null });
  await page.goto("/pulse");
  await openMission(page);
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await expect(page.getByTestId("mission-question")).toHaveCount(0);
});

test("every control clears 44px and a long option does not overflow its card", async ({
  page,
}) => {
  // On the PROJECT'S OWN viewport, so the phone case is really the phone. A long, honest option
  // label is the case that breaks: truncating it means the operator chooses something they
  // cannot read.
  await stub(page);
  await page.goto("/pulse");
  await openMission(page);
  const card = page.getByTestId("mission-question");
  await expect(card).toBeVisible();

  const controls = [
    ...(await page.getByTestId("mission-question-option").all()),
    page.getByTestId("mission-question-text"),
    page.getByTestId("mission-question-send"),
  ];
  for (const c of controls) {
    const box = await c.boundingBox();
    expect(box, "a control was not laid out").not.toBeNull();
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }

  const cardBox = (await card.boundingBox())!;
  for (const o of await page.getByTestId("mission-question-option").all()) {
    const b = (await o.boundingBox())!;
    expect(b.x + b.width).toBeLessThanOrEqual(cardBox.x + cardBox.width + 1);
  }
  // …and the page itself does not gain a horizontal scrollbar because of it.
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

// ==============================================================================================
// The states the card has over TIME — superseded, answered — and keyboard operability.
//
// #892 asks for the question to be *operable*, and a card is only operable if the operator can
// reach it from the keyboard and if what it shows is the question the server currently has. Both
// are states this suite could not see while every mock answered with one fixed body, so the
// mission detail is mutable from here down (#900 review, findings 5 and 8).
// ==============================================================================================

/** The replacement: the supervisor asked again about the same objective while the operator was
 *  typing, so question 41 is gone and 42 is the one holding. */
const SUPERSEDING = {
  seq: 42,
  question: "The branch moved. Is this mission still about the same PR?",
  objective: "pr_open",
  episode: 2,
  options: [
    { label: "Yes, carry on", action: "note_answer" },
    { label: "No — stop checking it", action: "stand_down_objective" },
  ],
};

/** Re-route the per-mission read so the console's own reload can see a DIFFERENT mission than the
 *  one it first painted. Registered after `stub`, so it wins (Playwright matches most-recent). */
async function mutableMission(page: Page, next: () => unknown) {
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, (r) =>
    r.fulfill({ json: next() }),
  );
}

function missionWith(question: unknown, events: unknown[] = []) {
  return {
    ...MISSION,
    id: "msn_1",
    title: "Ship it",
    needs_you: question !== null,
    needs_you_why: question === null ? [] : ["question"],
    question,
    events,
    events_next_seq: null,
  };
}

test("a SUPERSEDED question replaces the card, and the draft does not cross over", async ({
  page,
}) => {
  // The bug this pins: the card is not remounted when the prop swaps, so the free text typed
  // about question 41 stays in the box — enabled — over question 42, one click from being sent
  // as the answer to a question it was never about.
  await stub(page);
  let question: unknown = QUESTION;
  await mutableMission(page, () => missionWith(question));
  await page.route("**/api/missions/*/answer", (r) => {
    question = SUPERSEDING; // the server already moved on
    return r.fulfill({
      status: 409,
      json: {
        detail:
          "that question is no longer the open one — it was answered or superseded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await expect(page.getByTestId("mission-question")).toContainText(
    "Two pull requests",
  );

  await page
    .getByTestId("mission-question-text")
    .fill("the one from Tuesday, obviously");
  await page.getByTestId("mission-question-send").click();

  // The replacement is on screen...
  await expect(page.getByTestId("mission-question")).toContainText(
    "The branch moved",
  );
  await expect(page.getByTestId("mission-console")).toContainText(
    "no longer the open one",
  );
  // ...and the box is empty, because this is a different question.
  await expect(page.getByTestId("mission-question-text")).toHaveValue("");
  // The options are the NEW question's, not the old one's.
  await expect(page.getByTestId("mission-question-option")).toHaveCount(2);
});

test("an ANSWERED question leaves the answer on the thread and no card", async ({
  page,
}) => {
  await stub(page);
  let question: unknown = QUESTION;
  let events: unknown[] = [];
  await mutableMission(page, () => missionWith(question, events));
  await page.route("**/api/missions/*/answer", (r) => {
    question = null;
    events = [
      {
        seq: 42,
        at: T,
        kind: "answer",
        text: "waive_objective; rm -rf /",
        meta: { question_seq: 41, objective: "pr_open", action: "note_answer" },
      },
    ];
    return r.fulfill({
      json: {
        action: "note_answer",
        objective: "pr_open",
        answer: "waive_objective; rm -rf /",
        applied: "recorded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await page.getByTestId("mission-question-option").first().click();

  // The card goes — an answered question is not still asking.
  await expect(page.getByTestId("mission-question")).toHaveCount(0);
  // ...and the answer is history, rendered as TEXT. The label was never an instruction and it is
  // not one on the timeline either.
  await expect(page.getByTestId("mission-console")).toContainText("rm -rf");
  await expect(page.getByTestId("mission-console")).not.toContainText(
    "Needs your answer",
  );
});

test("the card is operable from the KEYBOARD, with a visible ring", async ({
  page,
}) => {
  await stub(page);
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/answer", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        action: "note_answer",
        objective: "pr_open",
        answer: "x",
        applied: "recorded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await expect(page.getByTestId("mission-question")).toBeVisible();

  // FOCUS MOVED BY THE KEYBOARD, not by `.focus()`. Chromium only matches `:focus-visible` on a
  // button when the focus came from a keyboard, so a programmatic focus would assert the ring is
  // absent on a card whose CSS is perfectly correct — a red that says nothing.
  await page.getByTestId("mission-question-text").click();
  await page.keyboard.press("Shift+Tab");

  const focused = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el) return null;
    const s = getComputedStyle(el);
    return {
      testid: el.getAttribute("data-testid"),
      index: el.getAttribute("data-index"),
      style: s.outlineStyle,
      width: s.outlineWidth,
    };
  });
  // Tabbing off the free-text box lands on the LAST option — the options really are in the tab
  // order, rather than being a mouse-only surface.
  expect(focused?.testid).toBe("mission-question-option");
  expect(focused?.style).not.toBe("none");
  expect(parseFloat(focused?.width ?? "0")).toBeGreaterThan(0);

  // ...and activating it from the keyboard behaves exactly as tapping does — including the
  // confirmation, because the option Shift+Tab lands on is a SETTLING one and a keyboard path
  // that skipped the second step would be a way round the very fence the card just grew.
  await page.keyboard.press("Enter");
  expect(bodies).toHaveLength(0);
  await page.keyboard.press("Enter");
  await expect.poll(() => bodies.length).toBe(1);
  expect((bodies[0] as Record<string, unknown>).option_index).toBe(
    QUESTION.options.length - 1,
  );
});

test("a LABEL cannot hide the consequence, and a settling option asks twice", async ({
  page,
}) => {
  // The model writes the label AND picks the action, and the operator sees only the label — so a
  // label reading "Keep working; leave this required" over a hidden `waive_objective` obtains a
  // confirmation under false pretences (#900 review 2, finding 1). "The label is never executed"
  // was the property the closed set was built for, and it is not the whole threat: a button that
  // lies still gets pressed.
  await stub(page);
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/answer", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        action: "waive_objective",
        objective: "pr_open",
        answer: "x",
        applied: "waived",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  const deceptive = page.getByTestId("mission-question-option").nth(1);
  await expect(deceptive).toContainText("Keep working");

  // THE SERVER'S OWN WORDS are on screen beside it, saying what the button really does.
  const card = page.getByTestId("mission-question");
  await expect(card).toContainText("Marks this objective NOT REQUIRED");
  // …and they are the server's: the label's own text is not what carries the meaning.
  await expect(
    page.getByTestId("mission-question-consequence").nth(1),
  ).toContainText("NOT REQUIRED");

  // A settling option ASKS TWICE. One tap arms it and sends nothing.
  await deceptive.click();
  await expect(deceptive).toHaveText("CONFIRM");
  expect(bodies).toHaveLength(0);
  // …AND THE ARMED STATE IS AUDIBLE (#900 review 8, finding 3). `aria-label` overrides the
  // button's text, so a screen reader kept hearing the model's label plus the consequence and
  // never heard that the consequential option was armed — the second tap, whose whole reason to
  // exist is to be an informed decision, announced nothing about being a confirmation.
  await expect(deceptive).toHaveAccessibleName(/^Confirm:/);

  // …the second tap is the one that acts, and it still sends only the INDEX.
  await deceptive.click();
  await expect.poll(() => bodies.length).toBe(1);
  expect((bodies[0] as Record<string, unknown>).option_index).toBe(1);
  expect(JSON.stringify(bodies[0])).not.toContain("waive_objective");
});

test("a NON-settling option acts on the first tap", async ({ page }) => {
  // The confirmation is for consequences, not a tax on every answer: `note_answer` records the
  // operator's choice and changes nothing else.
  await stub(page);
  const bodies: unknown[] = [];
  await page.route("**/api/missions/*/answer", (r) => {
    bodies.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        action: "note_answer",
        objective: "pr_open",
        answer: "x",
        applied: "recorded",
      },
    });
  });

  await page.goto("/pulse");
  await openMission(page);
  await page.getByTestId("mission-question-option").first().click();
  await expect.poll(() => bodies.length).toBe(1);
  expect((bodies[0] as Record<string, unknown>).option_index).toBe(0);
});
