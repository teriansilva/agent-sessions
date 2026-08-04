import { expect, test } from "@playwright/test";

// #781 — one card, one box, one statement.
//
// A card carrying an escalation used to render the same fact four times (title, review summary,
// orchestrator rationale, review reason, plus a ⚠ whose aria-label repeated the reason), link the
// same session twice (the row's `Open session` beside the card's `Jump in`), and draw the action
// as its OWN bordered box inside the card — a box in a box, each with its own footer.
//
// The box part is the reason this is a real-browser test and not only a jsdom one: "there is no
// second frame" is a computed-style fact (border width, background) and "the controls share a
// row" is a geometry fact. An emulator reports neither.

const NOW = Math.floor(Date.now() / 1000);

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

const ACTION = {
  id: "act-1",
  state: "escalated",
  ts: NOW - 600,
  expires_at: NOW + 1800,
  tier: "yolo",
  session_id: "claude:aaa",
  engine: "claude",
  title: "Switch the default model",
  project: "infra",
  project_id: "p1",
  verb: "escalate",
  confidence: 0.62,
  rationale: RATIONALE,
  evidence: "recap",
};

const CARD = {
  id: "claude:aaa",
  engine: "claude",
  title: "Switch the default model",
  cwd: "/home/u/infra",
  project: { kind: "project", id: "p1", name: "infra", color: "#ffb000" },
  state: "needs_you",
  live: false,
  last_activity: NOW - 720,
  last_mtime: NOW - 720,
  // Both model passes have something to say about this session — which is the whole point.
  intervention_required: true,
  intervention_reason: REASON,
  ai_summary: "Editing opencode.json",
  synthesis: "",
  pending_action: ACTION,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: {
          auto_enabled: false,
          interval_minutes: 30,
          window_days: 3,
          scan_depth: "slow",
          configured: true,
        },
        orchestrator: ORCH_CONFIG,
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
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
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: ORCH_CONFIG,
        pending: [ACTION],
        feed: [],
        expired_now: 0,
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [CARD],
      },
    }),
  );
});

test("the action is not a second box inside the card, and the card speaks once", async ({
  page,
}) => {
  await page.goto("/pulse");
  await expect(page.getByText(RATIONALE)).toBeVisible();

  // --- one statement -------------------------------------------------------------------
  // The review's reason said the same thing the orchestrator's rationale says; it no longer
  // repeats it, and neither does the ⚠ (whose accessible name carried that reason verbatim).
  await expect(page.getByText(REASON)).toHaveCount(0);
  await expect(
    page.getByRole("img", { name: /intervention required/i }),
  ).toHaveCount(0);
  // The review's SUMMARY is the third description of the same session — it stands down too, so
  // the card carries exactly one prose line. The title still gives the card its identity.
  await expect(page.getByText("Editing opencode.json")).toHaveCount(0);

  // --- one link ------------------------------------------------------------------------
  const toSession = page.locator('a[href="/s/claude/aaa"]');
  await expect(toSession).toHaveCount(1);
  await expect(toSession).toHaveAccessibleName(/jump into/i);
  // Anchored: the app shell's own nav carries an "Open session overview" link, which is not the
  // row's duplicate and must not be matched here.
  await expect(page.getByRole("link", { name: /^open session$/i })).toHaveCount(
    0,
  );

  // --- one box -------------------------------------------------------------------------
  // The real-browser part: the embedded row must draw NO frame of its own. Computed style, so
  // a stylesheet regression is caught rather than a class name that merely still exists.
  const row = page.locator('[class*="actEmbedded"]');
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
  // Fully transparent — the card's own background shows through, so there is no second surface.
  expect(box.bg).toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
  expect(box.padding).toBe("0px");

  // --- one footer ----------------------------------------------------------------------
  // The action's state survives, exactly once, folded into the card's own footer.
  await expect(page.getByText("escalated", { exact: true })).toHaveCount(1);

  // --- no wasted line ------------------------------------------------------------------
  // Geometry: the evidence disclosure and the decision control sit on the SAME row. The ✕ used
  // to own a line of its own under the RECAP button.
  const recap = page.getByRole("button", { name: /show recap/i });
  const dismiss = page.getByRole("button", {
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
  const body = page.getByText("the recap body");
  await expect(body).toBeVisible();
  const bb = await body.boundingBox();
  expect(bb!.y).toBeGreaterThan(midR);
});

test("a blank rationale keeps the review's reason — the card never says nothing", async ({
  page,
}) => {
  // `str(item.get("rationale") or "")` accepts an empty rationale and `ActionRow` renders no
  // line for it, so suppressing on the action alone would leave a card with controls and no
  // explanation at all.
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [{ ...CARD, pending_action: { ...ACTION, rationale: "" } }],
      },
    }),
  );
  await page.goto("/pulse");
  await expect(page.getByText(REASON)).toBeVisible();
  await expect(
    page.getByRole("img", { name: /intervention required/i }),
  ).toHaveCount(1);
  // Nothing from the action to say, so the summary is still the card's line.
  await expect(page.getByText("Editing opencode.json")).toBeVisible();
});
