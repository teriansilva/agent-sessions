import { expect, test, type Locator, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";
import { SPECIALISTS_PATH } from "../src/lib/routes";

/** Share bundles carry specialists (#1714 P2, boards A–E): export brings the pinned skills; the
 *  import preview settles what each specialist runs on before anything is created; a reused skill
 *  needs no tick; a choice the server refuses is shown on its row with nothing imported — desktop
 *  and a 320 px phone. The backend is mocked; every request the SPA sends is asserted. */

const sha = (c: string) => c.repeat(64);
const file = (path: string, text: string) => ({ path, size: text.length, text });

const CATALOG = {
  imports: { "specialist:intern": { bundle: sha("f"), revision: sha("e") } },
  items: [
    { kind: "skill", id: "tdd", title: "tdd", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "skill", id: "forgejo-pr-flow", title: "forgejo-pr-flow", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "specialist", id: "implementer", title: "Implementer", depends_on: ["skill:tdd", "skill:forgejo-pr-flow"], exportable: true, reason: null, source: "local" },
    {
      kind: "specialist",
      id: "reviewer",
      title: "Reviewer",
      depends_on: ["skill:code-review"],
      exportable: false,
      reason: "Skill code-review changed since version 2 was saved, so the version this specialist pins can't be exported. Open the specialist and save a new version, then export it.",
      source: "local",
    },
  ],
};

const job = (title: string, skills: string[], over: Record<string, unknown> = {}) => ({
  title,
  summary: "",
  capacity: 1,
  skills,
  suggests: { engine: "claude", model: "claude-opus-5", effort: null },
  settled: { engine: "claude", model: "claude-opus-5", effort: null },
  settle_reason: null,
  grade: null,
  ...over,
});

const PREVIEW = {
  token: "a".repeat(32),
  expires_at: "2026-10-29T10:27:00Z",
  bundle: { digest: "4be1" + "0".repeat(56) + "09cd", size: 18432, format: 1, created_at: "2026-10-29T10:12:00Z", app_version: "0.26.0", source: "Studio laptop", items: 4, files: 4 },
  items: [
    { key: "skill:tdd", kind: "skill", id: "tdd", title: "tdd", dest: "tdd", status: "reused", reason: null, depends_on: [], uses: [], files: [file("SKILL.md", "# tdd\n")] },
    { key: "skill:forgejo-pr-flow", kind: "skill", id: "forgejo-pr-flow", title: "forgejo-pr-flow", dest: "forgejo-pr-flow", status: "new", reason: null, depends_on: [], uses: [], files: [file("SKILL.md", "# PR flow\n")] },
    {
      key: "specialist:implementer",
      kind: "specialist",
      id: "implementer",
      title: "Implementer",
      dest: "implementer-2",
      status: "renamed",
      reason: null,
      depends_on: ["skill:tdd", "skill:forgejo-pr-flow"],
      uses: [],
      files: [file("specialist.json", '{"format": 1}\n')],
      specialist: job("Implementer", ["tdd", "forgejo-pr-flow"]),
    },
    {
      key: "specialist:reviewer",
      kind: "specialist",
      id: "reviewer",
      title: "Reviewer",
      dest: "reviewer",
      status: "new",
      reason: null,
      depends_on: ["skill:tdd"],
      uses: [],
      files: [file("specialist.json", '{"format": 1}\n')],
      specialist: job("Reviewer", ["tdd"], {
        suggests: { engine: "codex-api", model: "gpt-5", effort: "high" },
        settled: null,
        settle_reason: "Suggested codex-api · gpt-5 · high, but codex-api cannot start a session on this host. Choose what it runs on here.",
      }),
    },
  ],
};

type Writes = { path: string; body: unknown }[];

async function setup(page: Page, opts: { confirm?: "ok" | "changed" | "settle-once" | "pin-moved" } = {}) {
  await commonMocks(page);
  await mockRoster(page);
  const writes: Writes = [];
  let confirms = 0;
  await page.route("**/api/specialists", (r) =>
    r.fulfill({ json: { specialists: [], templates: [], max_instances: 6, live_total: 0 } }),
  );
  await page.route("**/api/share/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET")
      writes.push({ path, body: path.endsWith("/preview") ? (request.postDataBuffer()?.length ?? 0) : request.postDataJSON() });
    if (path.endsWith("/catalog")) return route.fulfill({ json: CATALOG });
    if (path.endsWith("/imports")) return route.fulfill({ json: { imports: {} } });
    if (path.endsWith("/export")) return route.fulfill({ body: "bundle-bytes", headers: { "content-type": "application/x-tar" } });
    if (path.endsWith("/import/preview")) return route.fulfill({ json: PREVIEW });
    if (path.endsWith("/import")) {
      confirms += 1;
      if (opts.confirm === "changed")
        return route.fulfill({ status: 409, json: { detail: "Your library changed since this preview.", reason: "changed" } });
      if (opts.confirm === "settle-once" && confirms === 1)
        return route.fulfill({
          status: 422,
          json: {
            detail: "Nothing was imported: what a specialist runs on doesn't work here. Change it, then import again.",
            reason: "settle",
            items: { "specialist:implementer": "claude doesn't offer the model claude-opus-5 now." },
          },
        });
      return route.fulfill({
        json: {
          token: PREVIEW.token,
          bundle: PREVIEW.bundle.digest,
          repreview: null,
          items: PREVIEW.items.map((i) => ({
            key: i.key,
            kind: i.kind,
            id: i.id,
            title: i.title,
            ...(opts.confirm === "pin-moved" && i.key === "specialist:implementer"
              ? {
                  status: "failed",
                  dest: i.dest,
                  reason:
                    "Nothing was written: a skill it pins changed or can't be read during the import. Preview the file again to import it.",
                  retry: false,
                }
              : { status: i.status === "reused" ? "reused" : "created", dest: i.dest, reason: null, retry: false }),
          })),
        },
      });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return writes;
}

test.beforeEach(async ({ page }, testInfo) => {
  if (testInfo.project.name === "mobile") await page.setViewportSize({ width: 320, height: 720 });
});

async function fits(page: Page, dialog: Locator) {
  const box = await dialog.boundingBox();
  const width = page.viewportSize()!.width;
  expect(box!.x).toBeGreaterThanOrEqual(-1);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  const overflow = await dialog.evaluate((el) => {
    const body = el.querySelector("[class*='body']") as HTMLElement | null;
    return body ? body.scrollWidth - body.clientWidth : 0;
  });
  expect(overflow).toBeLessThanOrEqual(1);
}

async function tall44(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(43.5);
}

async function openPreview(page: Page) {
  await page.goto(SPECIALISTS_PATH);
  await page.getByRole("button", { name: "Import…" }).click();
  const dialog = page.getByTestId("share-import");
  await dialog.getByTestId("share-file").setInputFiles({
    name: "review-team.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("bundle-bytes"),
  });
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  return dialog;
}

test("export: a specialist brings its pinned skills, locked; one whose pin is gone says how to fix it", async ({ page }) => {
  const writes = await setup(page);
  await page.goto(SPECIALISTS_PATH);
  await page.getByRole("button", { name: "Export…" }).click();
  const dialog = page.getByTestId("share-export");
  await dialog.getByLabel("Specialist Implementer").check();
  for (const sid of ["tdd", "forgejo-pr-flow"]) {
    await expect(dialog.getByLabel(`Skill ${sid} (included)`)).toBeDisabled();
    await expect(dialog.getByTestId(`share-export-row-skill:${sid}`)).toContainText("Included — pinned by implementer");
  }
  await expect(dialog.getByLabel("Specialist Reviewer")).toBeDisabled();
  await expect(dialog.getByTestId("share-export-row-specialist:reviewer")).toContainText("save a new version");
  await expect(dialog.getByText("What a specialist carries.")).toBeVisible();
  await fits(page, dialog);
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download bundle" }).click();
  await download;
  expect(writes).toEqual([{ path: "/api/share/export", body: { items: [{ kind: "specialist", id: "implementer" }] } }]);
});

test("import: an unavailable suggestion is settled in the preview; a reused skill needs no tick; confirm sends the choices", async ({
  page,
}) => {
  const writes = await setup(page);
  const dialog = await openPreview(page);
  await fits(page, dialog);
  const reused = dialog.getByTestId("share-preview-row-skill:tdd");
  await expect(reused).toHaveAttribute("data-state", "reused");
  await expect(reused).toContainText("already here as tdd");
  await expect(reused.getByRole("checkbox")).toHaveCount(0);
  const ready = dialog.getByTestId("share-settle-specialist:implementer");
  await expect(ready).toContainText("The suggested agent and model work here.");
  const reviewer = dialog.getByTestId("share-preview-row-specialist:reviewer");
  await expect(reviewer).toContainText("Choose agent");
  await expect(reviewer).toContainText("codex-api cannot start a session on this host");
  const importButton = dialog.getByRole("button", { name: "Import 3 items" });
  await expect(importButton).toBeDisabled();
  await expect(page.getByTestId("share-preview-summary")).toContainText("1 reused");
  await expect(page.getByTestId("share-preview-summary")).toContainText("1 needs an agent");
  const agent = reviewer.getByLabel("Agent for Reviewer");
  await tall44(agent);
  await agent.selectOption("codex");
  await expect(reviewer).toContainText("You chose codex. The file suggested codex-api · gpt-5 · high.");
  await expect(importButton).toBeEnabled();
  await fits(page, dialog);
  await importButton.click();
  await expect(dialog.getByTestId("share-result-row-skill:tdd")).toHaveAttribute("data-status", "reused");
  const created = dialog.getByTestId("share-result-row-specialist:implementer");
  await expect(created).toHaveAttribute("data-status", "created");
  await expect(created.getByRole("link", { name: /Open/ })).toHaveAttribute("href", "/library/specialists/implementer-2");
  const confirm = writes.find((w) => w.path === "/api/share/import");
  expect(confirm?.body).toEqual({
    token: PREVIEW.token,
    selection: ["skill:forgejo-pr-flow", "specialist:implementer", "specialist:reviewer"],
    settle: {
      "specialist:implementer": { engine: "claude", model: "claude-opus-5", effort: null },
      "specialist:reviewer": { engine: "codex", model: "default", effort: null },
    },
  });
});

test("import: a reused skill changed between preview and confirm asks to preview again", async ({ page }) => {
  await setup(page, { confirm: "changed" });
  const dialog = await openPreview(page);
  await dialog.getByTestId("share-preview-row-specialist:reviewer").getByLabel("Agent for Reviewer").selectOption("claude");
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  await expect(dialog.getByText(/Your library changed since this preview/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Preview again" })).toBeVisible();
  await expect(dialog.getByTestId("share-result-row-specialist:implementer")).toHaveCount(0);
});

test("import: all or nothing — one refused choice imports nothing, says which, focuses it, and the fix goes through", async ({
  page,
}) => {
  const writes = await setup(page, { confirm: "settle-once" });
  const dialog = await openPreview(page);
  await dialog.getByTestId("share-preview-row-specialist:reviewer").getByLabel("Agent for Reviewer").selectOption("claude");
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  const fields = dialog.getByTestId("share-settle-specialist:implementer");
  await expect(fields.getByRole("alert")).toContainText("claude doesn't offer the model claude-opus-5 now.");
  await expect(dialog.getByText(/Nothing was imported: what a specialist runs on/)).toBeVisible();
  await expect(fields.getByLabel("Agent for Implementer")).toBeFocused();
  await expect(dialog.getByTestId("share-result-row-specialist:implementer")).toHaveCount(0);
  await fields.getByLabel("Model for Implementer").selectOption("claude-sonnet-5");
  await expect(fields.getByRole("alert")).toHaveCount(0);
  // The confirm control stays reachable on the smallest phone.
  const importButton = dialog.getByRole("button", { name: "Import 3 items" });
  await importButton.scrollIntoViewIfNeeded();
  await expect(importButton).toBeInViewport();
  await importButton.click();
  await expect(dialog.getByTestId("share-result-row-specialist:implementer")).toHaveAttribute("data-status", "created");
  const confirms = writes.filter((w) => w.path === "/api/share/import").map((w) => w.body as { settle: Record<string, { model: string }> });
  expect(confirms).toHaveLength(2);
  expect(confirms[1].settle["specialist:implementer"].model).toBe("claude-sonnet-5");
  await fits(page, dialog);
});

test("import: a pinned skill that moved during the import is settled for good; the way on is Preview again", async ({ page }) => {
  const writes = await setup(page, { confirm: "pin-moved" });
  const dialog = await openPreview(page);
  await dialog.getByTestId("share-preview-row-specialist:reviewer").getByLabel("Agent for Reviewer").selectOption("claude");
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  const failed = dialog.getByTestId("share-result-row-specialist:implementer");
  await expect(failed).toHaveAttribute("data-status", "failed");
  await expect(failed).toContainText("Preview the file again to import it.");
  await expect(dialog.getByRole("button", { name: "Retry failed items" })).toHaveCount(0);
  const again = dialog.getByRole("button", { name: "Preview again" });
  await tall44(again);
  await again.click();
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  expect(writes.filter((w) => w.path.endsWith("/preview"))).toHaveLength(2);
  expect(writes.filter((w) => w.path === "/api/share/import")).toHaveLength(1);
  await fits(page, dialog);
});
