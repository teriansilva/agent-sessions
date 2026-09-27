/** The playbook editor on a phone (#892, #900 review 2, finding 5).
 *
 * `docs/design.md` sets a >=44px floor for touch targets and the repo holds every operator
 * surface to it. This form was sized by padding alone — around 33px — and it is the form that
 * decides what a mission CHECKS before calling itself done, so a mis-tap here is not cosmetic.
 *
 * A real browser, because the property is laid-out geometry: a jsdom test cannot see a
 * `min-height` that a media query supplies.
 */
import { expect, test } from "@playwright/test";
import { CHECKLISTS_PATH } from "../src/lib/routes";

const CONFIG = {
  csrf: "x",
  new_session_engines: [],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  onboarded: true,
  mission_playbooks: {
    default_id: "ship",
    playbooks: [
      {
        id: "ship",
        label: "Ship it",
        objectives: [
          {
            key: "live",
            title: "It is live",
            probe: "http_status",
            probe_args: { url: "https://example.test/healthz" },
            gate: true,
          },
        ],
      },
    ],
  },
  mission_probes: {
    kinds: ["none", "http_status"],
    non_gating: [],
    args: {
      none: { required: [], optional: [] },
      http_status: { required: ["url"], optional: ["expect_status"] },
    },
    types: {
      none: {},
      http_status: { url: "text", expect_status: "int" },
    },
  },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) =>
    r.fulfill({
      json: { auto_update: true, current: "test", channel: "stable" },
    }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/ai-review/models**", (r) =>
    r.fulfill({ json: { models: [] } }),
  );
  await page.route("**/api/prompts**", (r) =>
    r.fulfill({ json: { prompts: [] } }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
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
});

test("every playbook control clears 44px on a phone and stays inside the viewport", async ({
  page,
}, testInfo) => {
  // THE TOUCH PROJECT ONLY, because the contract is about touch targets and the rule that
  // supplies the height is `@media (pointer: coarse)`. Running it under `desktop` — a real mouse
  // at a narrow viewport — would assert a floor the design system does not claim there, and the
  // failure would say nothing about a phone.
  test.skip(
    testInfo.project.name !== "mobile",
    "the 44px floor is a coarse-pointer contract",
  );
  // The panel is its own page under Missions → Checklists, beside the console whose missions
  // start from it (it was Settings → AI → Checklists until then).
  await page.goto(CHECKLISTS_PATH);
  const panel = page.getByTestId("mission-playbooks");
  await expect(panel).toBeVisible();

  // A DRAFT ROW TOO, because its id input only exists once one is added — the hidden state is
  // where a control gets added without anyone re-checking the floor.
  await page.getByTestId("playbook-add").click();
  await expect(page.getByTestId("playbook-id")).toBeVisible();

  const found = await panel.evaluate((root) => {
    const sel =
      "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])";
    const bad: string[] = [];
    let seen = 0;
    for (const el of Array.from(root.querySelectorAll<HTMLElement>(sel))) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      // The checkbox is sized by the browser; its 44px target is the label around it, which is
      // measured on its own below.
      if ((el as HTMLInputElement).type === "checkbox") continue;
      seen += 1;
      const label = `${el.tagName}.${el.className}`.slice(0, 60);
      if (r.height < 44) bad.push(`${label} h=${Math.round(r.height)}`);
      if (r.right > window.innerWidth + 1 || r.left < -1) {
        bad.push(`${label} x=${Math.round(r.left)}..${Math.round(r.right)}`);
      }
    }
    return { bad, seen };
  });

  // An inventory that measured nothing passes vacuously.
  expect(found.seen).toBeGreaterThan(8);
  expect(found.bad).toEqual([]);

  // …AND THE CHECKBOX'S OWN TARGET, which the sweep above skips (#900 review 7, finding 11).
  //
  // Skipping it and promising a follow-up measurement that did not exist meant the gate — the
  // one control whose meaning is "this can block the mission from ever finishing" — had NO floor
  // at all: the native box is capped at 16px so it does not shove the label out of line, so the
  // tap area is the `<label>`, and nothing measured it. Removing the floor left the test green.
  const gateLabel = page.getByTestId("objective-gate-label").first();
  await expect(gateLabel).toBeVisible();
  const box = await gateLabel.boundingBox();
  expect(box).not.toBeNull();
  expect(Math.round(box!.height)).toBeGreaterThanOrEqual(44);
  // …and it is the target: a tap anywhere in it flips the checkbox.
  const gate = page.getByTestId("objective-gate").first();
  const before = await gate.isChecked();
  await gateLabel.click({ position: { x: 10, y: box!.height - 4 } });
  expect(await gate.isChecked()).toBe(!before);
});

test("a REFUSED save shows the server's exact words and keeps the draft", async ({
  page,
}) => {
  // The refusal is the operator's ONLY instruction for fixing a bad template — it names the id,
  // the key and the reason — so paraphrasing it into "something went wrong" throws away the one
  // useful thing about it. And a refused save must not eat the draft: the whole point is that
  // they now go and correct it (#900 review 3, finding 5).
  await page.goto(CHECKLISTS_PATH);
  const panel = page.getByTestId("mission-playbooks");
  await expect(panel).toBeVisible();

  let sent = 0;
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    sent += 1;
    return r.fulfill({
      status: 422,
      json: {
        detail:
          "objective 'live' has invalid probe arguments — probe http_status: " +
          "expect_status must be an integer",
      },
    });
  });

  // A value the server refuses: the field is typed `int`, and a half-typed one is sent as text
  // so the refusal names it rather than the editor quietly dropping it.
  await page.getByTestId("objective-arg-expect_status").fill("2xx");
  await page.getByTestId("playbook-save").click();
  await expect.poll(() => sent).toBe(1);

  // THE SERVER'S OWN WORDS, verbatim.
  await expect(page.getByTestId("playbook-error")).toContainText(
    "expect_status must be an integer",
  );
  await expect(page.getByTestId("playbook-error")).toContainText("'live'");

  // …and the draft is still there to correct.
  await expect(page.getByTestId("objective-arg-expect_status")).toHaveValue(
    "2xx",
  );
});
