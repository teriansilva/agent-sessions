import { expect, test, type Page } from "@playwright/test";

import { setupBench } from "./terminal/harness";

// #781 — one card, one box, one statement.
//
// A card carrying an escalation used to render the same fact four times (title, review summary,
// orchestrator rationale, review reason, plus a ⚠ whose aria-label repeated the reason), link the
// same session twice (the row's `Open session` beside the card's `Jump in`), and draw the action
// as its OWN bordered box inside the card — a box in a box, each with its own footer.
//
// WHERE IT RENDERS (#948 P3): the card for a session no mission holds lived in the "Sessions
// without a mission" view. That view is gone; the decision renders in the SESSION's own pane
// (`session-decisions`), with `ActionRow` embedded exactly as it was inside the card. The strip is
// the card's successor, so every "once" below is asserted on it.
//
// The box part is the reason this is a real-browser test and not only a jsdom one: "there is no
// second frame" is a computed-style fact (border width, background) and "the controls share a
// row" is a geometry fact. An emulator reports neither.

const NOW = Math.floor(Date.now() / 1000);

const UUID = "aaaaaaaa-0000-4000-8000-000000000781";
const KEY = `claude:${UUID}`;

const ORCH_CONFIG = {
  enabled: true,
  autonomy: "suggest",
  allowed_verbs: ["continue"],
  auto_verbs_ceiling: ["continue"],
  confidence_min: 0.75,
  interval_minutes: 10,
  max_actions_per_pass: 4,
  proposal_ttl_minutes: 30,
  nudge_template: "Please continue.",
  prompt: "p",
  notify: "escalations",
  configured: true,
  default_prompt: "p",
  default_nudge_template: "Please continue.",
};

const RATIONALE = "Below the act threshold, so this one is yours to call.";
const REASON = "Agent blocked on user choice between 3 options";
const SUMMARY = "Editing opencode.json";

const ACTION = {
  id: "act-1",
  state: "escalated",
  ts: NOW - 600,
  expires_at: NOW + 1800,
  tier: "yolo",
  session_id: KEY,
  engine: "claude",
  title: "Switch the default model",
  project: "infra",
  project_id: "p1",
  verb: "escalate",
  confidence: 0.62,
  rationale: RATIONALE,
  evidence: "recap",
};

/** The session's own row, carrying BOTH model passes' words about it — which is the whole point:
 *  the review has something to say (summary + reason) and so does the orchestrator (rationale). */
const ROW = {
  id: KEY,
  engine: "claude",
  uuid: UUID,
  short_uuid: "aaaaaaaa",
  cwd: "/home/u/infra",
  project: { kind: "project", id: "p1", name: "infra" },
  last_mtime: NOW - 720,
  first_user_message: "",
  title: ACTION.title,
  sticky: false,
  archived: false,
  ai_summary: SUMMARY,
  ai_title: ACTION.title,
  intervention_required: true,
  intervention_reason: REASON,
  reviewed_at: NOW - 720,
  review_excluded: false,
  has_draft: false,
};

async function openPane(page: Page, pending: unknown[]) {
  await setupBench(page, {
    sessions: [{ engine: "claude", uuid: UUID, title: ACTION.title }],
  });
  // Only the LIST endpoint is overridden, so the bench's history/draft handling stays intact.
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [ROW],
        next_offset: null,
        total: 1,
        facets: { projects: [ROW.project], engines: ["claude"] },
      },
    }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending,
        feed: [],
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
  await page.goto(`/s/claude/${UUID}`);
  const strip = page.getByTestId("session-decisions");
  await expect(strip).toBeVisible();
  return strip;
}

test("the action is not a second box inside its host, and the host speaks once", async ({
  page,
}) => {
  const strip = await openPane(page, [ACTION]);
  await expect(strip.getByText(RATIONALE)).toBeVisible();
  await expect(page.getByText(RATIONALE)).toHaveCount(1);

  // --- one statement -------------------------------------------------------------------
  // The review's reason says the same thing the orchestrator's rationale says; the decision does
  // not repeat it, and neither does a ⚠ (whose accessible name carried that reason verbatim). The
  // review's SUMMARY is the third description of the same session — it stands down too, so the
  // decision carries exactly one prose line.
  await expect(strip.getByText(REASON)).toHaveCount(0);
  await expect(
    strip.getByRole("img", { name: /intervention required/i }),
  ).toHaveCount(0);
  await expect(strip.getByText(SUMMARY)).toHaveCount(0);

  // --- one link ------------------------------------------------------------------------
  // The pane IS the session, so the decision links to it zero times — no `Jump in`, and no
  // `Open session` footer link beside it.
  await expect(strip.locator(`a[href="/s/claude/${UUID}"]`)).toHaveCount(0);
  await expect(strip.getByRole("link", { name: /^open session$/i })).toHaveCount(
    0,
  );

  // --- one box -------------------------------------------------------------------------
  // The real-browser part: the embedded row must draw NO frame of its own. Computed style, so
  // a stylesheet regression is caught rather than a class name that merely still exists.
  const row = strip.locator('[class*="actEmbedded"]');
  await expect(row).toHaveCount(1);
  const box = await row.evaluate((el) => {
    const s = getComputedStyle(el);
    return {
      borderTop: s.borderTopWidth,
      borderRight: s.borderRightWidth,
      borderBottom: s.borderBottomWidth,
      borderLeft: s.borderLeftWidth,
      bg: s.backgroundColor,
      padding: s.padding,
    };
  });
  expect(box.borderTop).toBe("0px");
  expect(box.borderRight).toBe("0px");
  expect(box.borderBottom).toBe("0px");
  expect(box.borderLeft).toBe("0px");
  // Fully transparent — the host's own background shows through, so there is no second surface.
  expect(box.bg).toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
  expect(box.padding).toBe("0px");

  // --- one footer ----------------------------------------------------------------------
  // The action's state survives, exactly once. Embedded, `ActionRow` drops its own footer on the
  // promise that its host folds the state into the host's footer (#781).
  await expect(strip.getByText("escalated", { exact: true })).toHaveCount(1);

  // --- no wasted line ------------------------------------------------------------------
  // Geometry: the evidence disclosure and the decision control sit on the SAME row. The ✕ used
  // to own a line of its own under the RECAP button.
  const recap = strip.getByRole("button", { name: /show recap/i });
  const dismiss = strip.getByRole("button", {
    name: /dismiss this escalation/i,
  });
  const [rb, db] = [await recap.boundingBox(), await dismiss.boundingBox()];
  expect(rb).not.toBeNull();
  expect(db).not.toBeNull();
  const midR = rb!.y + rb!.height / 2;
  const midD = db!.y + db!.height / 2;
  // Same row: their vertical centres agree to within a few px, and the ✕ is to the RIGHT.
  expect(Math.abs(midR - midD)).toBeLessThan(6);
  expect(db!.x).toBeGreaterThan(rb!.x + rb!.width - 1);

  // The disclosure still works, and its body opens BELOW the shared row at full width.
  await page.route(/\/api\/pulse\/evidence/, (r) =>
    r.fulfill({
      json: { available: true, kind: "recap", text: "the recap body" },
    }),
  );
  await recap.click();
  const body = strip.getByText("the recap body");
  await expect(body).toBeVisible();
  // Scroll it in before measuring, and re-read the BUTTON in the same frame, so the two boxes are
  // taken after the same scroll — "below the row", not "happens to be on screen".
  await body.scrollIntoViewIfNeeded();
  // The disclosure renames itself on open, so re-locate it across both states rather than
  // reusing the /show recap/ locator, which stops matching the moment it is clicked.
  const recapOpen = strip.getByRole("button", { name: /(show|hide) recap/i });
  const [rb2, bb] = [await recapOpen.boundingBox(), await body.boundingBox()];
  expect(bb!.y).toBeGreaterThan(rb2!.y + rb2!.height / 2);
});

test("a blank rationale keeps the review's reason — a decision never says nothing", async ({
  page,
}) => {
  // `str(item.get("rationale") or "")` accepts an empty rationale and `ActionRow` renders no
  // line for it, so suppressing the review's reason on the action alone would leave controls with
  // no explanation at all. The card fell back to the review's reason; its successor must too.
  //
  // (The card's ⚠ image and its summary line were decorations of the card itself, removed with the
  // untracked view in #948 P3 — only the fallback, which is the guarantee, is asserted here.)
  const strip = await openPane(page, [{ ...ACTION, rationale: "" }]);
  await expect(
    strip.getByRole("button", { name: /dismiss this escalation/i }),
  ).toBeVisible();
  await expect(strip.getByText(REASON)).toBeVisible();
});
