/** HQ imports a skill from a repository link (#2145), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that Save stays unavailable until every file is on screen;
 *  that the source, the files table (size, lines, the script flagged in words) and the third-party
 *  caution sit where the operator decides; that a SKILL.md longer than anything HQ reads of a file
 *  (8,000 characters) is shown whole, every byte, once unfolded; that a 90-character path wraps
 *  inside its cell while size and lines keep their columns and nothing scrolls sideways; that a
 *  refused import offers nothing and says why in the warn tone; and that the saved skill's detail
 *  says "From a repository (unverified)" with no update offer. The byte-for-byte SAVE through the
 *  real server, from a real bare repository, is pinned in tests/test_ask_skill_import.py.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  decideRoute,
  minTarget,
  noSidewaysScroll,
  open,
  PID,
  shell,
} from "./askOperationShell";

const SKILL =
  "---\nname: pdf-forms\ndescription: Fill and flatten PDF forms from structured data.\n---\n\n# PDF forms\n" +
  Array.from(
    { length: 160 },
    (_, i) => `Step ${i + 1}: map field ${i + 1} to the value exactly as the form prints it.`,
  ).join("\n") +
  "\n";
const FILL = "#!/usr/bin/env python3\n\"\"\"Fill a PDF form from JSON.\"\"\"\nimport json\n";
const LONG_PATH =
  "reference/government-forms/eu/de/bundesagentur-fuer-arbeit/antrag-alg2-2026-formularfelder.md";
const SOURCE = "github.com/acme/agent-skills @ 3f9c2a1 · skills/pdf-forms/";

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "skill.import",
    risk: "reversible",
    label: "Import skill",
    verb: "Save skill",
    title: "pdf-forms",
    text: "Saves a new skill, “pdf-forms” (3 files, 13.0 KB), to your Library. A skill is an inert copy until a playbook or project uses it.",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    content: true,
    ...over,
  };
}

const CONTENT = {
  kind: "import",
  source: SOURCE,
  rows: [
    { path: "SKILL.md", bytes: SKILL.length, lines: 167, flag: "" },
    { path: LONG_PATH, bytes: 1126, lines: 27, flag: "" },
    { path: "scripts/fill.py", bytes: FILL.length, lines: 3, flag: "script" },
  ],
  files: [
    { path: "SKILL.md", text: SKILL },
    { path: LONG_PATH, text: "# Antrag ALG II (2026)\n\nField names as the form prints them.\n" },
    { path: "scripts/fill.py", text: FILL },
  ],
  refused: [],
};

/** The content route, held until `release()` — Save must stay unavailable until then. */
async function heldContent(page: Page, body: Record<string, unknown>) {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route(`**/api/ask/proposals/${PID}/content`, async (r) => {
    await gate;
    await r.fulfill({ json: body });
  });
  return release;
}

test("an import card shows the source, every file and the caution; Save waits for the files (#2145)", async ({
  page,
}) => {
  expect(SKILL.length).toBeGreaterThan(8000);
  const fake = await shell(page, card());
  const release = await heldContent(page, CONTENT);
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Saved to your Library. Open it in the Library.",
    library: { kind: "skill", id: "pdf-forms" },
  });
  const c = await open(page);
  const save = c.getByTestId("ask-operation-confirm");
  await expect(c.getByTestId("ask-library-loading")).toBeVisible();
  await expect(save).toHaveText("Save skill");
  await expect(save).toBeDisabled();
  release();

  await expect(c.getByTestId("ask-import-source")).toHaveText(SOURCE);
  await expect(save).toBeEnabled();
  const rows = c.getByTestId("ask-import-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(2)).toContainText("scripts/fill.py");
  await expect(rows.nth(2)).toContainText("Script");
  await expect(c.getByTestId("ask-import-caution")).toContainText(
    "Third-party content: agents will read these instructions. Review before saving.",
  );
  await expect(c.getByTestId("ask-import-caution")).toContainText("scripts/fill.py");

  // Every byte of a SKILL.md longer than HQ ever reads of a file, once unfolded.
  const skill = c.getByTestId("ask-library-file").first();
  await expect(skill).not.toContainText("Step 160");
  const more = c.getByTestId("ask-library-more").first();
  await minTarget(more);
  await more.focus();
  await page.keyboard.press("Enter");
  await expect(more).toHaveAttribute("aria-expanded", "true");
  expect(await skill.evaluate((el) => el.textContent)).toBe(SKILL);

  // The caution sits directly above Save: the last thing before the decision.
  const caution = (await c.getByTestId("ask-import-caution").boundingBox())!;
  const saveBox = (await save.boundingBox())!;
  expect(caution.y + caution.height).toBeLessThanOrEqual(saveBox.y);
  await minTarget(save);
  await minTarget(c.getByTestId("ask-operation-dismiss"));
  await noSidewaysScroll(page);

  await save.click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", { timeout: 20_000 });
  expect(bodies).toEqual([{ decision: "confirm" }]);
  await expect(c.getByTestId("ask-library-open")).toHaveAttribute(
    "href",
    "/library/skills/pdf-forms",
  );
});

test("a long third-party path wraps inside its cell; size and lines keep their columns (#2145)", async ({
  page,
}) => {
  await shell(page, card());
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) => r.fulfill({ json: CONTENT }));
  const c = await open(page);
  const table = c.getByTestId("ask-import-files");
  await expect(table).toBeVisible();
  const row = c.getByTestId("ask-import-row").nth(1);
  // Never ellipsized: the whole path is there to review.
  await expect(row.locator("td").first()).toHaveText(LONG_PATH);
  const geometry = await table.evaluate((t) => {
    const cells = [...t.querySelectorAll("tbody tr")[1].querySelectorAll("td")];
    const box = t.getBoundingClientRect();
    return {
      overflow: t.scrollWidth - t.clientWidth,
      right: box.right,
      cells: cells.map((cell) => cell.getBoundingClientRect()),
      pathHeight: cells[0].getBoundingClientRect().height,
      sizeHeight: cells[1].scrollHeight,
    };
  });
  expect(geometry.overflow).toBeLessThanOrEqual(0);
  for (const cell of geometry.cells) expect(cell.right).toBeLessThanOrEqual(geometry.right + 0.5);
  // The path wrapped onto more lines than its size cell needs.
  expect(geometry.pathHeight).toBeGreaterThan(geometry.sizeHeight);
  await expect(row.locator("td").nth(1)).toHaveText("1.1 KB");
  // A size never wraps: it stays one line in its column, on a phone too.
  const sizeLines = await row.locator("td").nth(1).evaluate((cell) => {
    // The text's own line boxes: the cell itself is as tall as the wrapped path beside it.
    const range = document.createRange();
    range.selectNodeContents(cell);
    return new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size;
  });
  expect(sizeLines).toBe(1);
  await expect(row.locator("td").nth(2)).toHaveText("27");
  await noSidewaysScroll(page);
});

test("a refused import offers nothing and says why, in the warn tone (#2145)", async ({ page }) => {
  await shell(
    page,
    card({
      state: "refused",
      title: "pdf-forms",
      text: "“pdf-forms” can't be saved as it is: nothing was saved.",
      summary:
        "pdf-forms can't be saved: assets/form.pdf is binary; reference/huge.md is 412.0 KB (limit 128 KB). Nothing was saved.",
      can_confirm: false,
      can_dismiss: false,
    }),
  );
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) =>
    r.fulfill({
      json: {
        kind: "import",
        source: "github.com/acme/agent-skills-legacy @ 51d0a8e · skills/pdf-forms/",
        rows: [
          { path: "SKILL.md", bytes: 3891, lines: 88, flag: "" },
          { path: "assets/form.pdf", bytes: 98304, lines: null, flag: "binary" },
          { path: "reference/huge.md", bytes: null, lines: null, flag: "oversize" },
        ],
        files: [],
        refused: ["assets/form.pdf is binary", "reference/huge.md is 412.0 KB (limit 128 KB)"],
      },
    }),
  );
  const c = await open(page);
  await expect(c).toHaveAttribute("data-state", "refused");
  const outcome = c.getByTestId("ask-operation-outcome");
  await expect(outcome).toContainText("Refused");
  await expect(outcome).toContainText("assets/form.pdf is binary");
  await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await expect(c.getByTestId("ask-operation-dismiss")).toHaveCount(0);
  const rows = c.getByTestId("ask-import-row");
  await expect(rows.nth(1)).toContainText("Binary");
  await expect(rows.nth(2)).toContainText("Over 128 KB");
  await expect(c.getByTestId("ask-import-caution")).toHaveCount(0);
  // Warn, not red: the card's edge is the degraded tone, never the down tone.
  const edge = await c.evaluate((el) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--status-degraded)";
    el.appendChild(probe);
    const want = getComputedStyle(probe).color;
    probe.remove();
    return { have: getComputedStyle(el).borderLeftColor, want };
  });
  expect(edge.have).toBe(edge.want);
  await noSidewaysScroll(page);
});

test("the saved skill says where it came from, unverified, with no update offer (#2145)", async ({
  page,
}) => {
  await shell(page, card());
  const skill = {
    id: "pdf-forms",
    revision: "r".repeat(64),
    files: { "SKILL.md": SKILL },
    gallery_origin: null,
    repo_origin: {
      url: "https://github.com/acme/agent-skills",
      commit: "3f9c2a1b7e4d5f6a7b8c9d0e1f2a3b4c5d6e7f80",
      path: "skills/pdf-forms",
      unverified: true,
    },
    editable: true,
  };
  await page.route("**/api/library/skills**", (r) => {
    const path = new URL(r.request().url()).pathname;
    return r.fulfill({ json: path === "/api/library/skills" ? { skills: [skill] } : skill });
  });
  await page.goto("/library/skills/pdf-forms");
  const origin = page.getByTestId("repo-origin");
  await expect(origin).toContainText(
    "From a repository (unverified) · github.com/acme/agent-skills @ 3f9c2a1 · skills/pdf-forms/",
  );
  await expect(origin.getByRole("link")).toHaveAttribute(
    "href",
    "https://github.com/acme/agent-skills",
  );
  await expect(page.getByTestId("update-available")).toHaveCount(0);
  await expect(page.getByText("From the gallery")).toHaveCount(0);
  await noSidewaysScroll(page);
});
