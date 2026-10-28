import { expect, test, type Locator, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { PLAYBOOKS_PATH, SKILLS_PATH } from "../src/lib/routes";

/** Share bundles (#1714 P1): export with the dependency closure, import preview with a rename,
 *  a refused dependency and an invalid item, confirm and the result — desktop and a 320 px phone. */

const CATALOG = {
  imports: { "skill:tdd": { bundle: "f".repeat(64), revision: "e".repeat(64) } },
  items: [
    { kind: "skill", id: "research-with-citations", title: "research-with-citations", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "skill", id: "research-review", title: "research-review", depends_on: [], exportable: true, reason: null, source: "local" },
    { kind: "skill", id: "tdd", title: "tdd", depends_on: [], exportable: true, reason: null, source: "local" },
    {
      kind: "playbook",
      id: "research-brief",
      title: "Research brief",
      depends_on: ["skill:research-with-citations", "skill:research-review"],
      exportable: true,
      reason: null,
      source: "local",
    },
  ],
};
const file = (path: string, text: string) => ({ path, size: text.length, text });
const PREVIEW = {
  token: "a".repeat(32),
  expires_at: "2026-10-27T10:15:00Z",
  bundle: { digest: "9f3c" + "0".repeat(56) + "a71e", size: 41984, format: 1, created_at: "2026-10-26T14:02:00Z", app_version: "0.24.1", source: "Studio laptop", items: 4, files: 4 },
  items: [
    { key: "skill:research-with-citations", kind: "skill", id: "research-with-citations", title: "research-with-citations", dest: "research-with-citations", status: "new", reason: null, depends_on: [], uses: [], files: [file("SKILL.md", "# Research with citations\n")] },
    { key: "skill:research-review", kind: "skill", id: "research-review", title: "research-review", dest: "research-review-2", status: "renamed", reason: null, depends_on: [], uses: [], files: [file("SKILL.md", "# Research review\n")] },
    { key: "playbook:research-brief", kind: "playbook", id: "research-brief", title: "Research brief", dest: "research-brief", status: "new", reason: null, depends_on: ["skill:research-with-citations", "skill:research-review"], uses: [{ from: "research-review", to: "research-review-2" }], files: [file("playbook.toml", "format = 5\n")] },
    { key: "playbook:triage", kind: "playbook", id: "triage", title: "Old triage", dest: null, status: "refused", reason: "not a valid playbook here: flows/main.toml: steps[2].after names an unknown step", depends_on: [], uses: [], files: [] },
  ],
};

type Writes = { path: string; body: unknown }[];

async function setup(
  page: Page,
  opts: {
    confirm?: "ok" | "changed" | "fail-once" | "fail-then-expire" | "lost-once";
    preview?: "ok" | "refused" | "omitted";
    imports?: "ok" | "fail";
  } = {},
) {
  let confirms = 0;
  let previews = 0;
  await commonMocks(page);
  const writes: Writes = [];
  await page.route("**/api/library/skills", (r) =>
    r.fulfill({
      json: {
        skills: [
          { id: "tdd", revision: "e".repeat(64), files: { "SKILL.md": "# tdd" }, gallery_origin: null, editable: true },
        ],
      },
    }),
  );
  await page.route("**/api/share/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      const raw = request.postDataBuffer();
      writes.push({ path, body: path.endsWith("/preview") ? raw?.length ?? 0 : request.postDataJSON() });
    }
    if (path.endsWith("/catalog")) return route.fulfill({ json: CATALOG });
    if (path.endsWith("/imports"))
      return opts.imports === "fail"
        ? route.fulfill({ status: 503, json: { detail: "sharing is busy; try again" } })
        : route.fulfill({ json: { imports: CATALOG.imports } });
    if (path.endsWith("/export"))
      return route.fulfill({ body: "bundle-bytes", headers: { "content-type": "application/x-tar" } });
    if (path.endsWith("/import/preview")) {
      if (opts.preview === "refused")
        return route.fulfill({
          status: 422,
          json: { detail: 'member: "skill/tdd/../../.bashrc" contains a . or .. segment', reason: "invalid" },
        });
      previews += 1;
      // Like the server: every preview after the first is a fresh token; another file is another
      // bundle (digest).
      const other = (request.postDataBuffer()?.toString() ?? "").includes("other");
      const token = previews === 1 ? PREVIEW.token : "b".repeat(30) + String(previews).padStart(2, "0");
      const bundle = other ? { ...PREVIEW.bundle, digest: "1".repeat(64) } : PREVIEW.bundle;
      const items =
        opts.preview === "omitted"
          ? PREVIEW.items.map((i) =>
              i.key === "skill:research-review"
                ? { ...i, files: [{ path: "SKILL.md", size: 2_000_000, omitted: true }] }
                : i,
            )
          : PREVIEW.items;
      return route.fulfill({ json: { ...PREVIEW, token, bundle, items } });
    }
    if (path.endsWith("/import")) {
      if (opts.confirm === "changed")
        return route.fulfill({
          status: 409,
          json: { detail: "Your library changed since this preview.", reason: "changed" },
        });
      const selection = (request.postDataJSON() as { selection: string[] }).selection;
      confirms += 1;
      if (opts.confirm === "lost-once" && confirms === 1) return route.abort("failed");
      if (opts.confirm === "fail-then-expire" && confirms === 2)
        return route.fulfill({
          json: {
            token: PREVIEW.token,
            bundle: PREVIEW.bundle.digest,
            repreview: "expired",
            items: PREVIEW.items.map((i) => ({
              key: i.key,
              kind: i.kind,
              id: i.id,
              title: i.title,
              ...(i.key === "playbook:research-brief"
                ? { status: "stale", dest: null, reason: "This preview expired." }
                : i.status === "refused"
                  ? { status: "refused", dest: null, reason: i.reason }
                  : { status: "created", dest: i.dest, reason: null }),
            })),
          },
        });
      const failing =
        (opts.confirm === "fail-once" || opts.confirm === "fail-then-expire") && confirms === 1;
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
            ...(i.status === "refused"
              ? { status: "refused", dest: null, reason: i.reason }
              : !selection.includes(i.key)
                ? { status: "not_selected", dest: null, reason: null }
                : i.key === "playbook:research-brief" && !selection.includes("skill:research-review")
                  ? { status: "refused", dest: null, reason: "needs skill research-review, which isn't being imported" }
                  : failing && i.key === "playbook:research-brief"
                    ? {
                        status: "failed",
                        dest: i.dest,
                        reason: "Nothing was written: a storage error (EIO). Confirm again to retry this item.",
                      }
                    : { status: "created", dest: i.dest, reason: null }),
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

async function target44(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(43.5);
  expect(box!.width).toBeGreaterThanOrEqual(43.5);
}

test("export: a chosen playbook locks in its skills and downloads exactly the chosen items", async ({ page }) => {
  const writes = await setup(page);
  await page.goto(SKILLS_PATH);
  await expect(page.getByTestId("imported-from-file")).toBeVisible();
  await page.getByRole("button", { name: "Export…" }).click();
  const dialog = page.getByTestId("share-export");
  await expect(dialog).toBeVisible();
  const playbook = dialog.getByLabel("Playbook Research brief");
  await target44(page.locator("label[for='share-x-playbook:research-brief']"));
  await playbook.check();
  const skill = dialog.getByLabel("Skill research-review (included)");
  await expect(skill).toBeChecked();
  await expect(skill).toBeDisabled();
  await expect(dialog.getByTestId("share-export-row-skill:research-review")).toContainText(
    "Included — needed by research-brief",
  );
  await expect(page.getByTestId("share-export-summary")).toContainText("3 items · 1 chosen · 2 included");
  await fits(page, dialog);
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download bundle" }).click();
  expect((await download).suggestedFilename()).toMatch(/^battlelab-\d{8}-\d{6}\.battlelab$/);
  expect(writes).toEqual([{ path: "/api/share/export", body: { items: [{ kind: "playbook", id: "research-brief" }] } }]);
  // Unticking the playbook releases its skills.
  await playbook.uncheck();
  await expect(dialog.getByLabel("Skill research-review")).toBeEnabled();
});

async function openPreview(page: Page) {
  await page.getByRole("button", { name: "Import…" }).click();
  const dialog = page.getByTestId("share-import");
  await dialog.getByTestId("share-file").setInputFiles({
    name: "research-kit.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("bundle-bytes"),
  });
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  return dialog;
}

test("import: preview shows the rename and refusals; a deselected dependency refuses its dependent; confirm sends the selection", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  const dialog = await openPreview(page);
  await fits(page, dialog);
  const renamed = dialog.getByTestId("share-preview-row-skill:research-review");
  await expect(renamed).toContainText("Renamed");
  await expect(renamed).toContainText("research-review → research-review-2");
  await expect(dialog.getByTestId("share-preview-row-playbook:research-brief")).toContainText(
    "uses research-review-2",
  );
  await expect(dialog.getByTestId("share-preview-row-playbook:triage")).toContainText("steps[2].after");
  await expect(dialog.getByLabel("Import Playbook Old triage")).toBeDisabled();
  await target44(page.locator("label[for='share-i-skill:research-review']"));
  // Deselect the renamed skill: the playbook that needs it is refused, with the reason.
  await dialog.getByLabel("Import Skill research-review").uncheck();
  const dependent = dialog.getByTestId("share-preview-row-playbook:research-brief");
  await expect(dependent).toHaveAttribute("data-state", "refused");
  await expect(dependent).toContainText("Needs skill research-review, which isn't being imported");
  await expect(page.getByTestId("share-preview-summary")).toContainText("1 to import");
  await dialog.getByRole("button", { name: "Import 1 item" }).click();
  const created = dialog.getByTestId("share-result-row-skill:research-with-citations");
  await expect(created).toHaveAttribute("data-status", "created");
  await expect(created.getByRole("link", { name: /Open/ })).toHaveAttribute(
    "href",
    "/library/skills/research-with-citations",
  );
  await expect(dialog.getByTestId("share-result-row-playbook:research-brief")).toHaveAttribute(
    "data-status",
    "refused",
  );
  const confirm = writes.find((w) => w.path === "/api/share/import");
  expect(confirm?.body).toEqual({
    token: PREVIEW.token,
    selection: ["skill:research-with-citations", "playbook:research-brief"],
  });
  await fits(page, dialog);
});

test("import: a library change after the preview asks to preview again", async ({ page }) => {
  await setup(page, { confirm: "changed" });
  await page.goto(SKILLS_PATH);
  const dialog = await openPreview(page);
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  await expect(dialog.getByText(/Your library changed since this preview/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Preview again" })).toBeVisible();
});

test("import: a hostile file is refused, and the dialog says nothing was read", async ({ page }) => {
  await setup(page, { preview: "refused" });
  await page.goto(SKILLS_PATH);
  await page.getByRole("button", { name: "Import…" }).click();
  const dialog = page.getByTestId("share-import");
  await dialog.getByTestId("share-file").setInputFiles({
    name: "evil.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("x"),
  });
  const alert = dialog.getByRole("alert");
  await expect(alert).toContainText("contains a . or .. segment");
  await expect(alert).toContainText("Nothing was read into your library");
  await fits(page, dialog);
});

test("import: the board B → E scenario — a failed item is retried under the same preview, never re-imported", async ({
  page,
}) => {
  const writes = await setup(page, { confirm: "fail-once" });
  await page.goto(SKILLS_PATH);
  const dialog = await openPreview(page);
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  const failed = dialog.getByTestId("share-result-row-playbook:research-brief");
  await expect(failed).toHaveAttribute("data-status", "failed");
  await expect(failed).toContainText("storage error (EIO)");
  await expect(dialog.getByRole("status")).toContainText("2 of the 3 items you chose were imported");
  await expect(dialog.getByTestId("share-result-row-playbook:triage")).toHaveAttribute("data-status", "refused");
  await target44(dialog.getByRole("button", { name: "Retry failed items" }));
  await dialog.getByRole("button", { name: "Retry failed items" }).click();
  await expect(failed).toHaveAttribute("data-status", "created");
  await expect(dialog.getByRole("status")).toContainText("3 of the 3 items you chose were imported");
  const confirms = writes.filter((w) => w.path === "/api/share/import");
  expect(confirms).toHaveLength(2);
  expect(confirms[0].body).toEqual(confirms[1].body); // same token, same selection: a retry
  expect(writes.filter((w) => w.path.endsWith("/preview"))).toHaveLength(1);
  await fits(page, dialog);
});

test("import: after a partial import, Preview again never re-selects the copies already made", async ({ page }) => {
  const writes = await setup(page, { confirm: "fail-then-expire" });
  await page.goto(SKILLS_PATH);
  const dialog = await openPreview(page);
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  await expect(dialog.getByTestId("share-result-row-playbook:research-brief")).toHaveAttribute("data-status", "failed");
  await dialog.getByRole("button", { name: "Retry failed items" }).click();
  await expect(dialog.getByText("Some items need a new preview.")).toBeVisible();
  await dialog.getByRole("button", { name: "Preview again" }).click();
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
  for (const label of ["Import Skill research-with-citations", "Import Skill research-review"])
    await expect(dialog.getByLabel(label)).not.toBeChecked();
  await expect(dialog.getByTestId("share-preview-row-skill:research-review")).toContainText(
    "Already imported from this file as research-review-2",
  );
  // The playbook failed, so whether it landed is unknown: nothing is ticked, and the preview says so.
  await expect(dialog.getByLabel("Import Playbook Research brief")).not.toBeChecked();
  await expect(dialog.getByText("An earlier import of this file may have been applied.")).toBeVisible();
  await dialog.getByLabel("Import Playbook Research brief").check();
  const playbook = dialog.getByTestId("share-preview-row-playbook:research-brief");
  await expect(playbook).toHaveAttribute("data-state", "refused");
  await expect(playbook).toContainText("which was already imported as research-with-citations");
  await expect(dialog.getByRole("button", { name: /Import \d/ })).toBeDisabled();
  expect(writes.filter((w) => w.path === "/api/share/import")).toHaveLength(2);
  await fits(page, dialog);
});

test("import: a lost confirm answer freezes the selection and retries exactly it", async ({ page }) => {
  const writes = await setup(page, { confirm: "lost-once" });
  await page.goto(PLAYBOOKS_PATH);
  const dialog = await openPreview(page);
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  await expect(dialog.getByText("The import may have been applied.")).toBeVisible();
  await expect(dialog.getByLabel("Import Skill research-review")).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Back" })).toHaveCount(0);
  await target44(dialog.getByRole("button", { name: "Try again (same selection)" }));
  await dialog.getByRole("button", { name: "Try again (same selection)" }).click();
  await expect(dialog.getByTestId("share-result-row-skill:research-review")).toHaveAttribute("data-status", "created");
  const confirms = writes.filter((w) => w.path === "/api/share/import");
  expect(confirms).toHaveLength(2);
  expect(confirms[0].body).toEqual(confirms[1].body);
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  await fits(page, dialog);
});

async function partial(page: Page) {
  const dialog = await openPreview(page);
  await dialog.getByRole("button", { name: "Import 3 items" }).click();
  await expect(dialog.getByTestId("share-result-row-playbook:research-brief")).toHaveAttribute("data-status", "failed");
  return dialog;
}

async function pick(dialog: Locator, body: string) {
  await dialog.getByTestId("share-file").setInputFiles({
    name: "research-kit.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from(body),
  });
  await expect(dialog.getByText("Not signed — imported from a file.")).toBeVisible();
}

async function nothingCreatedIsTicked(dialog: Locator) {
  for (const label of ["Import Skill research-with-citations", "Import Skill research-review"])
    await expect(dialog.getByLabel(label)).not.toBeChecked();
  await expect(dialog.getByTestId("share-preview-row-skill:research-review")).toContainText(
    "Already imported from this file as research-review-2",
  );
}

test("import: after a partial import, Back / another file / close and reopen never re-tick created copies", async ({
  page,
}) => {
  await setup(page, { confirm: "fail-once" });
  await page.goto(SKILLS_PATH);
  let dialog = await partial(page);
  // Close and reopen: the page remembers what this file created.
  await dialog.getByRole("button", { name: "Done" }).click();
  await page.getByRole("button", { name: "Import…" }).click();
  dialog = page.getByTestId("share-import");
  await pick(dialog, "bundle-bytes");
  await nothingCreatedIsTicked(dialog);
  // Back, then the same file again.
  await dialog.getByRole("button", { name: "Back" }).click();
  await pick(dialog, "bundle-bytes");
  await nothingCreatedIsTicked(dialog);
  // Another file (another bundle) starts fresh; the same file afterwards still remembers.
  await dialog.getByRole("button", { name: "Back" }).click();
  await pick(dialog, "other-bundle-bytes");
  await expect(dialog.getByLabel("Import Skill research-review")).toBeChecked();
  await dialog.getByRole("button", { name: "Back" }).click();
  await pick(dialog, "bundle-bytes");
  await nothingCreatedIsTicked(dialog);
  await fits(page, dialog);
});

test("import: a file too large to review needs an explicit acknowledgement before Import", async ({ page }) => {
  await setup(page, { preview: "omitted" });
  await page.goto(SKILLS_PATH);
  const dialog = await openPreview(page);
  const row = dialog.getByTestId("share-preview-row-skill:research-review");
  await row.getByText(/Review 1 file/).click();
  await expect(row).toContainText("can't be reviewed before you import it");
  const go = dialog.getByRole("button", { name: "Import 3 items" });
  await expect(go).toBeDisabled();
  const ack = dialog.getByTestId("share-blind-ack");
  await target44(ack);
  await ack.locator("input").check();
  await expect(go).toBeEnabled();
  await fits(page, dialog);
});

test("import: when the library's import record can't be read, nothing starts ticked", async ({ page }) => {
  await setup(page, { imports: "fail" });
  await page.goto(SKILLS_PATH);
  const dialog = await openPreview(page);
  await expect(dialog.getByText("Couldn't check whether this file was imported before.")).toBeVisible();
  for (const label of ["Import Skill research-with-citations", "Import Skill research-review", "Import Playbook Research brief"])
    await expect(dialog.getByLabel(label)).not.toBeChecked();
});
