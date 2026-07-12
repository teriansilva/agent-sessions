import { expect, test } from "@playwright/test";

// Real-browser guard for #476: Pulse cards and Overview session chips are framed by the
// HudFrame corner brackets ONLY — no engine/urgency `border-left` rail, so the bracketed left
// edge never doubles up into a "double line". A jsdom test can't prove this (no real layout /
// computed border geometry), which is exactly the failure mode docs/agent-workflow.md §5 calls
// out — so this asserts the *computed* border is uniform on all four sides while the four
// brackets are still present. Was RED before the fix (border-left: 3px ≠ 1px), GREEN after.
// Network is mocked so both surfaces render without a backend.

const now = Math.floor(Date.now() / 1000);

const config = {
  csrf: "x",
  new_session_engines: [],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

/** All four borders equal width ⇒ no left rail. Before the fix borderLeft was 3px. */
async function bordersUniform(locator: import("@playwright/test").Locator) {
  return locator.evaluate((el) => {
    const s = getComputedStyle(el);
    return {
      left: s.borderLeftWidth,
      right: s.borderRightWidth,
      top: s.borderTopWidth,
      bottom: s.borderBottomWidth,
    };
  });
}

test.describe("HUD card frame — brackets only, no left rail (#476)", () => {
  test("Pulse card: uniform border + four corner brackets", async ({ page }) => {
    const card = {
      id: "claude:need-1",
      engine: "claude",
      title: "Pulse needs-you card",
      cwd: "/home/u/proj",
      project: { kind: "folder", id: "/home/u/proj", name: "proj" },
      last_activity: now,
      ai_summary: "Awaiting a merge decision",
      intervention_required: true,
      intervention_reason: "Confirm the push",
      reviewed_at: now,
      live: false,
      state: "needs_you",
      synthesis: null,
    };
    await page.route("**/api/config", (r) => r.fulfill({ json: config }));
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
    // Empty sidebar so the only matching <li> is the Pulse card.
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
    );
    await page.route("**/api/pulse", (r) =>
      r.fulfill({
        json: {
          cache_version: 1,
          generated_at: now,
          window_days: 1,
          scan_depth: "fast",
          input_fingerprint: "fp",
          synthesis_skipped: false,
          banner: null,
          cards: [card],
        },
      }),
    );

    await page.goto("/pulse");

    const li = page.locator("li", { hasText: "Pulse needs-you card" });
    await expect(li).toBeVisible();

    const b = await bordersUniform(li);
    expect(b.left).toBe(b.top);
    expect(b.left).toBe(b.right);
    expect(b.left).toBe(b.bottom);

    await expect(li.locator(".hud-cnr")).toHaveCount(4);
  });

  test("Overview chip: uniform border + four corner brackets", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "mobile", "overview canvas is a desktop interaction");
    const sessions = [
      {
        id: "claude:aaa",
        engine: "claude",
        uuid: "aaa",
        short_uuid: "aaa",
        cwd: "/home/u/proj",
        project: { kind: "folder", id: "/home/u/proj", name: "proj" },
        last_mtime: now,
        first_user_message: "",
        title: "Overview chip session",
        sticky: false,
        archived: false,
      },
    ];
    await page.route("**/api/config", (r) => r.fulfill({ json: config }));
    await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
    await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
    await page.route(/\/api\/folders(\?.*)?$/, (r) =>
      r.fulfill({ json: { folders: [{ cwd: "/home/u/proj", label: "proj" }] } }),
    );
    await page.route("**/api/sessions**", (r) =>
      r.fulfill({
        json: {
          sessions,
          next_offset: null,
          total: sessions.length,
          facets: { projects: [{ kind: "folder", id: "/home/u/proj", name: "/home/u/proj" }], engines: ["claude"] },
        },
      }),
    );

    await page.goto("/overview");
    const ov = page.locator(".tr-overview");
    // Unadopted sessions fold into the synthetic Default project; expand it to render chips.
    await ov.getByTitle(/expand Default/i).click();

    const chip = ov.locator(".tr-ov-chip").first();
    await expect(chip).toBeVisible();

    const b = await bordersUniform(chip);
    expect(b.left).toBe(b.top);
    expect(b.left).toBe(b.right);
    expect(b.left).toBe(b.bottom);

    await expect(chip.locator(".hud-cnr")).toHaveCount(4);
  });
});
