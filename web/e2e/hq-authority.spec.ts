/** Settings → AI → HQ → Authority (#2390, epic #2070 A1), in a real browser on desktop and mobile:
 *  the presets and per-class Ask / Act controls save exactly what was chosen against the read
 *  revision; a stale revision says so and offers Reload; RISKY rows are stamped and a class HQ
 *  can't act on yet says its choice is kept; every control is a 44 px target on a phone and the
 *  page never scrolls sideways. And a card HQ ran itself reads "Done by HQ", with no buttons.
 *  Screenshots in both themes go to the test's output dir (the issue and PR attach them). */
import { expect, test, type Page } from "@playwright/test";

import { mockShell } from "./callFixture";
import { open as openCard, PID, shell } from "./askOperationShell";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
/** The page scrolls inside its own panel: a tall window lets one shot hold the whole section. */
async function tall(page: Page) {
  const vp = page.viewportSize()!;
  if (vp.height < 2200) await page.setViewportSize({ width: vp.width, height: 2200 });
}
type Classes = Record<string, "ask" | "act">;
const ALL = ["reversible", "starts", "destructive", "external", "widens"];

function derive(classes: Classes): string {
  const acts = ALL.filter((c) => classes[c] === "act");
  if (acts.length === 0) return "ask_first";
  if (acts.length === ALL.length) return "full";
  if (acts.length === 1 && acts[0] === "reversible") return "standard";
  return "custom";
}

async function authority(page: Page, start: Classes, opts: { conflict?: boolean; acts?: unknown[] } = {}) {
  const saves: Record<string, unknown>[] = [];
  let classes = { ...start };
  let revision = 4;
  const view = () => ({
    classes,
    preset: derive(classes),
    revision,
    actable: ["reversible"],
    risky: ["destructive", "external", "widens"],
  });
  await mockShell(page);
  await page.route("**/api/hq/authority", async (r) => {
    if (r.request().method() === "GET") return r.fulfill({ json: view() });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    saves.push(body);
    if (opts.conflict || body.revision !== revision)
      return r.fulfill({ status: 409, json: { detail: "authority changed; reload" } });
    if (typeof body.preset === "string") {
      const act = (c: string) =>
        body.preset === "full" || (body.preset === "standard" && c === "reversible");
      classes = Object.fromEntries(ALL.map((c) => [c, act(c) ? "act" : "ask"])) as Classes;
    } else classes = { ...classes, ...(body.classes as Classes) };
    revision += 1;
    return r.fulfill({ json: view() });
  });
  await page.route("**/api/hq/acts", (r) => r.fulfill({ json: { acts: opts.acts ?? [] } }));
  return saves;
}

const ASK: Classes = Object.fromEntries(ALL.map((c) => [c, "ask"])) as Classes;
const ACTS = [
  { ts: 1764102000, op: "session.rename", label: "Rename", title: "docs-site: rewrite the install guide", state: "done", outcome: "Renamed.", via: "typed", conversation: "c" },
  { ts: 1764101400, op: "session.tag", label: "Tag", title: "release train v0.24", state: "done", outcome: "Tag set.", via: "call", conversation: "c" },
  { ts: 1764100800, op: "project.color", label: "Project colour", title: "landing page", state: "stale", outcome: "", via: "typed", conversation: "c" },
];

for (const theme of ["dark", "light"] as const) {
  test(`authority: presets and class rows save, RISKY and locked shown · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, ASK);
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    await expect(section.getByTestId("hq-authority-preset-ask_first")).toHaveAttribute("aria-checked", "true");
    for (const c of ["destructive", "external", "widens"]) {
      await expect(section.getByTestId(`hq-authority-row-${c}`).getByText("RISKY")).toBeVisible();
    }
    await expect(section.getByTestId("hq-authority-row-reversible").getByText("RISKY")).toHaveCount(0);
    await expect(section.getByTestId("hq-authority-locked")).toContainText("This Authority setting");
    await expect(section.getByTestId("hq-acts-empty")).toBeVisible();
    if (PHONE(page)) {
      for (const b of await section.getByRole("radio").all()) {
        expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
    }
    const over = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(over).toBeLessThanOrEqual(0);
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-ask-first-${theme}.png`) });

    await section.getByTestId("hq-authority-preset-standard").click();
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0]).toEqual({ revision: 4, preset: "standard" });
    await expect(section.getByTestId("hq-authority-preset-standard")).toHaveAttribute("aria-checked", "true");
    await expect(section.getByRole("status")).toContainText("Saved");

    // The operator's own mix (#2070 direction): starts or sends acts too. Kept, still asks in A1.
    const starts = section.getByTestId("hq-authority-row-starts");
    await starts.getByRole("radio", { name: "Act" }).click();
    await expect.poll(() => saves.length).toBe(2);
    expect(saves[1]).toEqual({ revision: 5, classes: { starts: "act" } });
    await expect(section.getByTestId("hq-authority-custom")).toBeVisible();
    await expect(section.getByTestId("hq-authority-later-starts")).toContainText("Kept");
    await section.screenshot({ path: info.outputPath(`hq-authority-custom-${theme}.png`) });
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`authority: a stale revision says so and Reload reads it again · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, ASK, { conflict: true });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const section = page.getByTestId("hq-authority");
    await section.getByTestId("hq-authority-row-reversible").getByRole("radio", { name: "Act" }).click();
    await expect.poll(() => saves.length).toBe(1);
    await expect(section.getByRole("alert")).toContainText("changed elsewhere");
    await expect(section.getByTestId("hq-authority-preset-full")).toBeDisabled();
    await tall(page);
    await section.screenshot({ path: info.outputPath(`hq-authority-stale-${theme}.png`) });
    await section.getByRole("button", { name: "Reload" }).click();
    await expect(section.getByTestId("hq-authority-preset-full")).toBeEnabled();
  });
}

for (const theme of ["dark", "light"] as const) {
  test(`authority: recent acts list · ${theme}`, async ({ page }, info) => {
    await authority(page, { ...ASK, reversible: "act" }, { acts: ACTS });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const list = page.getByTestId("hq-acts");
    await expect(list.getByRole("listitem")).toHaveCount(3);
    await expect(list).toContainText("Rename · docs-site: rewrite the install guide");
    await expect(list).toContainText("on a call");
    await tall(page);
    await page.getByTestId("hq-authority").screenshot({ path: info.outputPath(`hq-authority-standard-acts-${theme}.png`) });
  });

  test(`a card HQ ran itself reads Done by HQ, with no buttons · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "operation",
      op: "session.rename",
      risk: "reversible",
      label: "Rename",
      verb: "Rename",
      title: "docs-site: rewrite the install guide",
      text: "Renames it to “Install guide v2”.",
      needs: [],
      state: "done",
      summary: "Renamed.",
      reason: "",
      can_confirm: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    const c = await openCard(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-operation-kicker")).toHaveText("Done by HQ");
    await expect(c.getByTestId("ask-operation-by-hq")).toContainText("Authority setting");
    await expect(c.getByRole("button")).toHaveCount(0);
    await c.screenshot({ path: info.outputPath(`hq-act-card-${theme}.png`) });
  });
}
