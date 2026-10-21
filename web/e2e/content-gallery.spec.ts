import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import {
  GALLERY_PATH,
  GALLERY_PLAYBOOKS_PATH,
  GALLERY_SKILLS_PATH,
  PLAYBOOKS_PATH,
  SKILLS_PATH,
} from "../src/lib/routes";

/** Library → Playbooks / Skills → Browse gallery (#1440, was Library → Gallery #1371/#1417). */
const digest = "a".repeat(64),
  playbookDigest = "e".repeat(64),
  revision = "b".repeat(64),
  nextRevision = "c".repeat(64);
const content = {
  format: 1,
  kind: "skill",
  id: "review-checklist",
  version: "1.0.0",
  title: "Review checklist",
  author: "<img src=x onerror=alert(1)>",
  publisher: "BattleLab",
  summary: "A small review checklist.",
  source: "https://example.com/source",
  requirements: ["Read the change first"],
  files: {
    "SKILL.md": "# Review\nRead the change and report concrete findings.",
    "notes/example.md": "Supporting notes",
  },
};
const entry = { content, digest, available: true };
const longFile = Array.from({ length: 125 }, (_, i) => `line ${i + 1} = "value ${i + 1}"`).join("\n");
const playbookEntry = {
  digest: playbookDigest,
  available: true,
  content: {
    ...content,
    kind: "playbook",
    id: "inbox-triage",
    title: "Inbox triage",
    version: "1.2.0",
    files: {
      "playbook.toml": `format = 1\n[identity]\nid = "inbox-triage"\n${longFile}`,
      "flows/triage.toml": 'format = 1\n[[steps]]\nid = "read"',
      "README.md": "Triage the inbox.",
    },
  },
};
const playbookPreview = {
  inputs: [{ name: "inbox_name", label: "Inbox", required: true }],
  flows: [
    {
      id: "triage",
      title: "Triage",
      steps: [
        {
          id: "read",
          title: "Read the new messages",
          actor: { kind: "agent", engine: "claude" },
          after: [],
          note: false,
        },
      ],
    },
  ],
};
const origin = {
  state: "verified",
  kind: "skill",
  title: content.title,
  id: content.id,
  version: "1.0.0",
  publisher: content.publisher,
  source: content.source,
  entry_digest: digest,
  base_revision: revision,
  pointer: "d".repeat(32),
};
type Options = {
  state?: string;
  lost?: boolean;
  conflict?: boolean;
  disappear?: boolean;
  collision?: boolean;
  importCollision?: boolean;
  otherFiles?: Record<string, string>;
  reservedFiles?: boolean;
  /** The public gallery's answer to the automatic check. */
  refresh?: "ok" | "unreachable" | "refused";
  /** Publish the skill as 2.0.0, newer than the local copy's 1.0.0 base. */
  newer?: boolean;
  playbooks?: boolean;
  /** After the first refresh, the stored-listing read fails until `control.catalogOk = true`. */
  failCatalogAfterRefresh?: boolean;
  /** The second stored-listing read (the one after the automatic check) waits for `control.release()`. */
  holdSecondRead?: boolean;
};
async function setup(page: Page, options: Options = {}) {
  await commonMocks(page);
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const refreshes: number[] = [];
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const control = { catalogOk: !options.failCatalogAfterRefresh, release: () => release() };
  let reads = 0;
  let current = {
    id: "my-skill",
    revision,
    files: { ...content.files },
    gallery_origin: origin,
    editable: true,
  };
  let updatedId: string | null = null;
  let editedCopy: typeof current | null = null;
  const occupied: string[] = [];
  let collision = options.collision;
  let importCollision = options.importCollision;
  let saved = false,
    conflict = options.conflict;
  const skill = options.newer
    ? { ...entry, content: { ...content, version: "2.0.0" } }
    : entry;
  const entries = [skill, ...(options.playbooks ? [playbookEntry] : [])];
  await page.route("**/api/gallery**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (request.method() !== "GET")
      writes.push({
        path,
        body: request.postData() ? request.postDataJSON() : {},
      });
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ status, json });
    const listing = () =>
      reply({
        state: options.state ?? "ready",
        entries: options.state === "empty" ? [] : entries,
        sequence: 1,
        digest,
        expires_at: 1792000000,
        error: options.state === "unavailable" ? "Gallery unavailable" : null,
      });
    if (path.endsWith("/refresh")) {
      // Like the server: a POST without the CSRF token is refused (#1440: the automatic check
      // must not race the app's first config read).
      if (!request.headers()["x-csrf-token"]) return reply({ detail: "forbidden" }, 403);
      refreshes.push(Date.now());
      if (options.refresh === "unreachable")
        return reply(
          { detail: "The public gallery can't be reached right now.", reason: "unreachable" },
          503,
        );
      if (options.refresh === "refused")
        return reply({ detail: "Invalid gallery descriptor." }, 422);
      return listing();
    }
    if (path === "/api/gallery") {
      reads += 1;
      if (options.holdSecondRead && reads >= 2) await held;
      if (!control.catalogOk && refreshes.length)
        return reply({ detail: "Gallery storage is unavailable." }, 503);
      return listing();
    }
    if (path.includes("/content/")) {
      const shown = path.includes(playbookDigest) ? playbookEntry : skill;
      return reply({
        entry: shown,
        importable: options.state !== "stale",
        reason: options.state === "stale" ? "Publication expired" : null,
        cut_digest: digest,
        preview: shown === playbookEntry ? playbookPreview : null,
      });
    }
    if (path.endsWith("/import")) {
      const body = request.postDataJSON();
      if (importCollision) {
        importCollision = false;
        occupied.push(body.id);
        return reply({ detail: "That local skill ID already exists." }, 409);
      }
      saved = true;
      current = { ...current, id: body.id };
      if (options.lost) return route.abort("failed");
      return reply({
        state: "complete",
        operation: body.operation,
        result: { kind: "skill", ...current },
      });
    }
    if (path.includes("/operations/"))
      return saved
        ? reply({
            state: "complete",
            operation: path.split("/").at(-1),
            result: { kind: "skill", ...current, id: updatedId ?? current.id },
          })
        : reply({ detail: "No gallery operation was recorded." }, 404);
    if (path.endsWith("/compare") && options.disappear && current.revision === nextRevision) {
      const reverted = { ...content.files, "SKILL.md": "Original" };
      return reply({
        kind: "skill", id: "my-skill", revision: nextRevision, origin: origin.pointer,
        incoming_digest: digest, comparison: nextRevision, base: reverted, local: reverted,
        incoming: reverted, proposed: reverted, conflicts: [], changed_files: [],
        incoming_version: "2.0.0", base_version: "1.0.0",
      });
    }
    if (path.endsWith("/compare") && options.reservedFiles) {
      const additions = Object.fromEntries(
        ["constructor", "toString", "__proto__"].map((name) => [name, `Incoming ${name}`]),
      );
      return reply({
        kind: "skill", id: "my-skill", revision: current.revision, origin: origin.pointer,
        incoming_digest: digest, comparison: current.revision, base: current.files, local: current.files,
        incoming: { ...current.files, ...additions }, proposed: { ...current.files, ...additions },
        conflicts: [], changed_files: Object.keys(additions), incoming_version: "2.0.0",
        base_version: "1.0.0",
      });
    }
    if (path.endsWith("/compare"))
      return reply({
        kind: "skill",
        id: "my-skill",
        revision: current.revision,
        origin: origin.pointer,
        incoming_digest: digest,
        comparison: current.revision,
        base: { "SKILL.md": "Original" },
        local: current.files,
        incoming: { "SKILL.md": "Incoming" },
        proposed: current.files,
        conflicts: ["SKILL.md"],
        changed_files: ["SKILL.md"],
        incoming_version: "2.0.0",
        base_version: "1.0.0",
      });
    if (path.endsWith("/update")) {
      const body = request.postDataJSON();
      if (collision) {
        collision = false;
        occupied.push(body.new_id);
        return reply({ detail: "That local skill ID already exists." }, 409);
      }
      if (conflict) {
        conflict = false;
        current = { ...current, revision: nextRevision };
        return reply({ detail: "Local content changed. Compare again." }, 409);
      }
      saved = true;
      updatedId = body.new_id;
      if (options.lost) return route.abort("failed");
      return reply({
        state: "complete",
        operation: body.operation,
        result: { kind: "skill", ...current, id: updatedId },
      });
    }
    return reply({ detail: "Unexpected gallery request" }, 500);
  });
  await page.route("**/api/library/skills**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.endsWith("/copies")) {
      writes.push({ path, body: request.postDataJSON() });
      if (conflict) {
        conflict = false;
        current = {
          ...current,
          revision: nextRevision,
          files: { ...current.files, ...options.otherFiles, "SKILL.md": "Changed by another editor" },
        };
        return route.fulfill({
          status: 409,
          json: { detail: "This skill changed; compare again." },
        });
      }
      saved = true;
      updatedId = request.postDataJSON().new_id;
      editedCopy = { ...current, id: updatedId!, files: request.postDataJSON().files, revision: nextRevision };
      if (options.lost) return route.abort("failed");
      return route.fulfill({ json: {
        state: "complete", operation: request.postDataJSON().operation,
        result: { kind: "skill", ...editedCopy },
      }});
    }
    if (path === "/api/library/skills")
      return route.fulfill({
        json: {
          skills: [
            current,
            ...(editedCopy ? [editedCopy] : []),
            ...occupied.map((id) => ({ ...current, id, gallery_origin: null })),
          ],
        },
      });
    const id = decodeURIComponent(path.split("/").at(-1)!);
    if (editedCopy && id === editedCopy.id) return route.fulfill({ json: editedCopy });
    if (id === current.id) return route.fulfill({ json: current });
    return route.fulfill({ status: 404, json: { detail: "No such skill." } });
  });
  await page.route(/\/api\/playbooks(\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        playbooks: [
          { id: "forgejo-workflow", ok: true, error: null, source: "bundled", editable: false, revision: null, default: false, name: "Forgejo workflow" },
        ],
        default: null,
        recovery_total: 0,
      },
    }),
  );
  return Object.assign(writes, { refreshes, control });
}
async function geometry(page: Page) {
  const contrast = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d")!;
    const color = (value: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = value;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const light = (c: number[]) =>
      c
        .slice(0, 3)
        .map((v) => v / 255)
        .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
        .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
    const failures = [];
    for (const el of document.querySelectorAll<HTMLElement>(
      "main h1,main h2,main p,main label,main button,main a,main summary,main input,main textarea,main code",
    )) {
      if (
        !el.checkVisibility() ||
        el.matches(":disabled") ||
        getComputedStyle(el).opacity !== "1"
      )
        continue;
      let ancestor: HTMLElement | null = el;
      let bg: number[] = [];
      while (ancestor) {
        bg = color(getComputedStyle(ancestor).backgroundColor);
        if (bg[3] === 255) break;
        ancestor = ancestor.parentElement;
      }
      if (!ancestor) continue;
      const fg = color(getComputedStyle(el).color),
        a = light(fg),
        b = light(bg),
        ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      if (ratio < 4.5)
        failures.push({
          text: (el.textContent ?? el.tagName).slice(0, 40),
          ratio,
        });
    }
    return failures;
  });
  expect(contrast).toEqual([]);

  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  const heights = await page
    .locator("main button,main summary,main a[class]")
    .evaluateAll((els) =>
      els
        .filter((el) => el.checkVisibility())
        .map((el) => el.getBoundingClientRect().height),
    );
  expect(heights.every((h) => h >= 44)).toBe(true);
  await page.locator("main a[class]").first().focus();
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe(
    "BODY",
  );
}
const technical = (page: Page) =>
  page.locator("main summary").filter({ hasText: /^Technical details$/ }).last();
const confirmDialog = (page: Page) => page.getByRole("dialog");

for (const theme of ["dark", "light"]) {
  test.describe(theme, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript(
        (value) => localStorage.setItem("tr-theme", value),
        theme,
      );
    });
    test("browse, review the complete content and add a skill with one confirmed click", async ({
      page,
    }, info) => {
      const writes = await setup(page);
      await page.goto(GALLERY_SKILLS_PATH);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(
        page.getByRole("heading", { name: "Review checklist", exact: true }),
      ).toBeVisible();
      await expect(page.getByRole("link", { name: "Browse gallery" })).toHaveAttribute("aria-current", "page");
      // The automatic check ran once; nobody pressed a refresh button.
      await expect.poll(() => writes.refreshes.length).toBe(1);
      await geometry(page);
      await page.getByLabel("Search the gallery").fill("no match");
      await expect(page.getByText("Nothing in the gallery matches this search.")).toBeVisible();
      await page.getByLabel("Search the gallery").fill("");
      await page.getByRole("link", { name: "View Review checklist" }).click();
      // Review comes first: requirements, then the first file already open in full.
      await expect(page.getByRole("heading", { name: "Requirements" })).toBeVisible();
      await expect(
        page.getByText("Read the change and report concrete findings.", { exact: false }),
      ).toBeVisible();
      expect(await page.locator("main img").count()).toBe(0);
      // Digests are tucked away, not gone.
      await expect(page.getByText(digest)).toBeHidden();
      await technical(page).click();
      await expect(page.getByText(digest)).toBeVisible();
      await technical(page).click();
      const add = page.getByRole("button", { name: "Add to my skills" });
      await expect(page.getByText("Adds as review-checklist.", { exact: true })).toBeVisible();
      await expect(add).toBeEnabled();
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`inspect-${theme}.png`), fullPage: true });
      await add.click();
      const dialog = confirmDialog(page);
      await expect(dialog.getByRole("heading", { name: "Add Review checklist to your skills?" })).toBeVisible();
      await expect(dialog).toContainText("Only add it if you've checked its requirements and files.");
      await page.screenshot({ path: info.outputPath(`confirm-${theme}.png`) });
      // Opening the confirmation writes nothing.
      expect(writes.filter((w) => w.path.endsWith("/import"))).toEqual([]);
      await dialog.getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(page.getByText("Added —", { exact: false })).toBeVisible();
      const imports = writes.filter((w) => w.path.endsWith("/import"));
      expect(imports).toHaveLength(1);
      expect(imports[0].body).toMatchObject({ kind: "skill", id: "review-checklist", digest, acknowledge: true });
      await page.getByRole("link", { name: "Open", exact: true }).click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toBeVisible();
    });
    test("playbook detail shows inputs, workflow and every file before Add", async ({
      page,
    }, info) => {
      await setup(page, { playbooks: true });
      await page.goto(GALLERY_PLAYBOOKS_PATH);
      // The playbook view lists playbooks only.
      await expect(page.getByRole("heading", { name: "Review checklist" })).toHaveCount(0);
      await page.getByRole("link", { name: "View Inbox triage" }).click();
      await expect(page.getByRole("heading", { name: "What's inside" })).toBeVisible();
      await expect(page.getByText("Inbox · required", { exact: true })).toBeVisible();
      await expect(page.getByText("Read the new messages · claude", { exact: true })).toBeVisible();
      await expect(page.getByText('id = "inbox-triage"', { exact: false })).toBeVisible();
      await page.getByRole("button", { name: "Expand all" }).click();
      await expect(page.getByText("Triage the inbox.", { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: "Add to my playbooks" })).toBeEnabled();
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`playbook-${theme}.png`), fullPage: true });
    });
    test("a lost response shows the recovery banner, blocks Add, and is checked — never replayed", async ({
      page,
    }) => {
      const writes = await setup(page, { lost: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
      await page.getByRole("button", { name: "Add to my skills" }).click();
      await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
      const banner = page.getByTestId("gallery-pending-operation");
      await expect(banner.getByRole("heading")).toHaveText(
        "We couldn't confirm whether adding Review checklist finished.",
      );
      // The banner is never folded away, and every write waits for it.
      await expect(banner.getByRole("button", { name: "Check status" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Add to my skills" })).toBeDisabled();
      await expect(page.getByText("Check the earlier attempt above first.")).toBeVisible();
      await page.reload();
      await expect(page.getByRole("button", { name: "Add to my skills" })).toBeDisabled();
      await page.getByRole("button", { name: "Check status" }).click();
      await expect(page.getByText("It was added.", { exact: false })).toBeVisible();
      expect(writes.filter((w) => w.path.endsWith("/import"))).toHaveLength(1);
    });
    test("an ID taken meanwhile is re-proposed and needs another click", async ({ page }) => {
      const writes = await setup(page, { importCollision: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
      await page.getByRole("button", { name: "Add to my skills" }).click();
      const dialog = confirmDialog(page);
      await dialog.getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(dialog.getByTestId("gallery-id-taken")).toHaveText(
        "review-checklist was just taken. It will be added as review-checklist-2 instead.",
      );
      // The refusal is never retried on its own.
      await page.waitForTimeout(500);
      expect(writes.filter((w) => w.path.endsWith("/import"))).toHaveLength(1);
      await expect(page.getByTestId("gallery-pending-operation")).toHaveCount(0);
      await dialog.getByRole("button", { name: "I've reviewed it — Add as review-checklist-2" }).click();
      await expect(page.getByText("Added —", { exact: false })).toBeVisible();
      expect(writes.filter((w) => w.path.endsWith("/import")).map((w) => w.body.id)).toEqual([
        "review-checklist",
        "review-checklist-2",
      ]);
    });
    test("local edits survive CAS conflict, compare and guarded navigation", async ({
      page,
    }, info) => {
      const writes = await setup(page, { conflict: true });
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await page.getByLabel("Your draft: SKILL.md").fill("My retained instructions");
      await page.getByRole("link", { name: "Back to skills", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Leave this draft?" })).toBeVisible();
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByRole("alert").first()).toContainText("This skill changed");
      await page.getByRole("button", { name: "Check status" }).click();
      await page.getByRole("button", { name: "Try again" }).click();
      await confirmDialog(page).getByRole("button", { name: "Yes, try again", exact: true }).click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue("My retained instructions");
      await page.getByRole("button", { name: "Compare stored version" }).click();
      await expect(page.getByText("Changed by another editor", { exact: true })).toBeVisible();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue("My retained instructions");
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`edit-conflict-${theme}.png`), fullPage: true });
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByText("Saved an edited copy.")).toBeVisible();
      expect(writes.at(-1)?.body.revision).toBe(nextRevision);
    });
    test("skill conflict comparison preserves unedited files from another editor", async ({ page }) => {
      const writes = await setup(page, {
        conflict: true,
        otherFiles: { "notes/new.md": "Added by another editor", "notes/example.md": "Updated by another editor" },
      });
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await page.getByLabel("Your draft: SKILL.md").fill("My instructions");
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByRole("alert").first()).toContainText("This skill changed");
      await page.getByRole("button", { name: "Check status" }).click();
      await page.getByRole("button", { name: "Try again" }).click();
      await confirmDialog(page).getByRole("button", { name: "Yes, try again", exact: true }).click();
      await page.getByRole("button", { name: "Compare stored version" }).click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue("My instructions");
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByText("Saved an edited copy.")).toBeVisible();
      expect(writes.filter(w => w.path.endsWith("/copies")).at(-1)?.body.files).toEqual({
        "SKILL.md": "My instructions", "notes/new.md": "Added by another editor",
        "notes/example.md": "Updated by another editor",
      });
    });
    test("skill edits create a named copy and keep the original unchanged", async ({ page }, info) => {
      const writes = await setup(page);
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await page.getByLabel("Your draft: SKILL.md").fill("My edited instructions");
      const id = page.getByRole("textbox", { name: "New copy ID", exact: true });
      const save = page.getByRole("button", { name: "Save edited copy", exact: true });
      await id.fill("my-skill");
      await expect(save).toBeDisabled();
      await id.fill("my-edited-skill");
      await save.scrollIntoViewIfNeeded();
      await page.screenshot({ path: info.outputPath(`skill-copy-action-${theme}.png`), fullPage: true });
      await save.click();
      await expect(page.getByRole("link", { name: "Open new copy", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/my-edited-skill`);
      await expect(save).toBeDisabled();
      const copies = writes.filter(w => w.path.endsWith("/copies"));
      expect(copies).toHaveLength(1);
      expect(copies[0].body).toMatchObject({ new_id: "my-edited-skill", origin: origin.pointer, revision, files: { ...content.files, "SKILL.md": "My edited instructions" } });
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`skill-edited-copy-${theme}.png`), fullPage: true });
      await page.getByRole("link", { name: "Open new copy", exact: true }).click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue("My edited instructions");
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue(content.files["SKILL.md"]);
    });
    test("skill edited copy checks a lost response without replaying the write", async ({ page }) => {
      const writes = await setup(page, { lost: true });
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await page.getByLabel("Your draft: SKILL.md").fill("My edited instructions");
      await page.getByRole("button", { name: "Save edited copy", exact: true }).click();
      await expect(page.getByRole("alert").first()).toBeVisible();
      await page.reload();
      await page.getByRole("button", { name: "Check status" }).click();
      await expect(page.getByRole("link", { name: "Open", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/my-skill-edited`);
      expect(writes.filter(w => w.path.endsWith("/copies"))).toHaveLength(1);
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue(content.files["SKILL.md"]);
    });
    test("a gallery update is added as a separately named copy", async ({ page }) => {
      const writes = await setup(page);
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
      await expect(page.getByText("v1.0.0 → v2.0.0", { exact: true })).toBeVisible();
      await expect(page.getByText("your current copy stays as it is.", { exact: false }).first()).toBeVisible();
      await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Reviewed copy");
      const add = page.getByRole("button", { name: "Add updated copy", exact: true });
      await expect(page.getByText("Adds as my-skill-v2-0-0.", { exact: true })).toBeVisible();
      await technical(page).click();
      const id = page.getByRole("textbox", { name: "New copy ID", exact: true });
      await id.fill("my-skill");
      await expect(add).toBeDisabled();
      await id.fill("my-reviewed-skill");
      await expect(add).toBeEnabled();
      await add.click();
      await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(page.getByRole("link", { name: "Open", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/my-reviewed-skill`);
      await expect(add).toHaveCount(0);
      expect(writes.filter(w => w.path.endsWith("/update"))).toHaveLength(1);
      expect(writes.find(w => w.path.endsWith("/update"))?.body).toMatchObject({ id: "my-skill", new_id: "my-reviewed-skill", acknowledge: true });
      await page.getByRole("link", { name: "my-skill", exact: true }).click();
      await expect(page.getByRole("textbox", { name: "Your draft: SKILL.md", exact: true })).toHaveValue(content.files["SKILL.md"]);
    });
    test("an updated copy recovers a lost response without creating it twice", async ({ page }) => {
      const writes = await setup(page, { lost: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
      await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Reviewed copy");
      await technical(page).click();
      await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("recovered-copy");
      await page.getByRole("button", { name: "Add updated copy" }).click();
      await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(page.getByTestId("gallery-pending-operation")).toBeVisible();
      await page.reload();
      await page.getByRole("button", { name: "Check status" }).click();
      await expect(page.getByRole("link", { name: "Open", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/recovered-copy`);
      expect(writes.filter(w => w.path.endsWith("/update"))).toHaveLength(1);
    });
    test("an occupied copy ID keeps the reviewed draft and is re-proposed, never retried", async ({ page }) => {
      const writes = await setup(page, { collision: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
      await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Retain this result");
      await technical(page).click();
      await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("occupied-copy");
      await page.getByRole("button", { name: "Add updated copy" }).click();
      const dialog = confirmDialog(page);
      await dialog.getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(dialog.getByTestId("gallery-id-taken")).toContainText("occupied-copy-2");
      await expect(page.getByLabel("Result for SKILL.md (you can edit it)")).toHaveValue("Retain this result");
      expect(writes.filter(w => w.path.endsWith("/update"))).toHaveLength(1);
      await dialog.getByRole("button", { name: "I've reviewed it — Add as occupied-copy-2" }).click();
      await expect(page.getByRole("link", { name: "Open", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/occupied-copy-2`);
      expect(writes.filter(w => w.path.endsWith("/update")).at(-1)?.body.files).toHaveProperty(["SKILL.md"], "Retain this result");
      // One refused write and one explicitly confirmed retry (#1417, #1440).
      expect(writes.filter(w => w.path.endsWith("/update")).map(w => w.body.new_id)).toEqual(["occupied-copy", "occupied-copy-2"]);
    });
    test("an update without conflicts is one summary line and one button; reserved filenames survive", async ({ page }) => {
      const writes = await setup(page, { reservedFiles: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
      await expect(page.getByTestId("gallery-update-summary")).toHaveText(
        "3 files change, none conflict with your edits",
      );
      // No three-way chooser without a conflict.
      await expect(page.getByRole("group", { name: /^Versions of / })).toHaveCount(0);
      await page.getByTestId("gallery-quiet-files").locator("summary").first().click();
      for (const name of ["constructor", "toString", "__proto__"]) {
        const file = page.getByTestId("gallery-quiet-files").locator("details").filter({ has: page.locator("summary", { hasText: new RegExp(`^${name}$`) }) });
        await file.locator("summary").click();
        await expect(file.getByText(`Incoming ${name}`, { exact: true })).toBeVisible();
      }
      await page.getByRole("button", { name: "Add updated copy" }).click();
      await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(page.getByText("Added the updated copy", { exact: false })).toBeVisible();
      const files = writes.find((w) => w.path.endsWith("/update"))?.body.files as Record<string, string>;
      for (const name of ["constructor", "toString", "__proto__"]) {
        expect(Object.hasOwn(files, name)).toBe(true);
        expect(files[name]).toBe(`Incoming ${name}`);
      }
    });
    test("skill editor can add reserved-property filenames", async ({ page }) => {
      const writes = await setup(page);
      await page.goto(`${SKILLS_PATH}/my-skill`);
      for (const name of ["constructor", "toString", "__proto__"]) {
        await page.getByLabel("New relative file path").fill(name);
        await page.getByRole("button", { name: "Add text file" }).click();
        await page.locator("summary").filter({ hasText: new RegExp(`^${name}$`) }).click();
        await page.getByLabel(`Your draft: ${name}`, { exact: true }).fill(`Local ${name}`);
      }
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByText("Saved an edited copy.")).toBeVisible();
      const files = writes.filter(w => w.path.endsWith("/copies")).at(-1)?.body.files as Record<string, string>;
      for (const name of ["constructor", "toString", "__proto__"]) {
        expect(Object.hasOwn(files, name)).toBe(true);
        expect(files[name]).toBe(`Local ${name}`);
      }
    });
    test("a conflict needs a result, and a stale save keeps the draft for Compare again", async ({
      page,
    }, info) => {
      const writes = await setup(page, { conflict: true });
      await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
      await expect(page.getByTestId("gallery-update-summary")).toHaveText(
        "1 file conflicts with your edits — choose a result",
      );
      for (const label of ["Your edit", "Gallery version", "Original"])
        await expect(page.getByRole("heading", { name: label, exact: true })).toBeVisible();
      const add = page.getByRole("button", { name: "Add updated copy" });
      await expect(add).toBeDisabled();
      await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Reviewed merge");
      await add.click();
      const dialog = confirmDialog(page);
      await dialog.getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(dialog.getByRole("alert")).toContainText("Local content changed");
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(page.getByLabel("Result for SKILL.md (you can edit it)")).toHaveValue("Reviewed merge");
      // The server answered and recorded nothing, so no recovery banner stands in the way.
      await expect(page.getByTestId("gallery-pending-operation")).toHaveCount(0);
      await page.getByRole("button", { name: "Compare again" }).click();
      // The draft is frozen until the new comparison is in.
      await expect(page.getByRole("button", { name: "Compare again" })).toBeEnabled();
      await expect(page.getByLabel("Result for SKILL.md (you can edit it)")).toHaveValue("Reviewed merge");
      await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Reviewed merge after comparison");
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`compare-${theme}.png`), fullPage: true });
      await add.click();
      await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
      await expect(page.getByText("Added the updated copy", { exact: false })).toBeVisible();
      expect(writes.filter((w) => w.path.endsWith("/update")).at(-1)?.body.comparison).toBe(nextRevision);
    });
    for (const state of ["empty", "stale", "unavailable"])
      test(`${state} gallery says so plainly and keeps the local library one tab away`, async ({
        page,
      }, info) => {
        await setup(page, { state });
        await page.goto(GALLERY_SKILLS_PATH);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await expect(page.getByRole("link", { name: "Your skills", exact: true })).toBeVisible();
        await expect(
          page.getByText(
            state === "empty"
              ? "Nothing has been published to the gallery yet."
              : state === "stale"
                ? "This gallery listing is out of date, so adding is paused until it refreshes."
                : "The gallery listing saved on this device can't be read.",
            { exact: false },
          ),
        ).toBeVisible();
        if (state === "stale") {
          await page.getByRole("link", { name: "View Review checklist" }).click();
          await expect(page.getByRole("button", { name: "Add to my skills" })).toBeDisabled();
        }
        await page.screenshot({ path: info.outputPath(`${state}-${theme}.png`), fullPage: true });
      });
    test("an unreachable gallery reads as one plain sentence with Try again (#1440)", async ({ page }, info) => {
      const writes = await setup(page, { state: "empty", refresh: "unreachable" });
      await page.goto(GALLERY_PLAYBOOKS_PATH);
      const callout = page.getByTestId("gallery-unreachable");
      await expect(callout.locator("p").first()).toHaveText(
        "The public gallery can't be reached right now. Your playbooks and skills are not affected.",
      );
      // The old doubled message is gone.
      await expect(page.getByText("retained", { exact: false })).toHaveCount(0);
      await expect(page.getByTestId("gallery-empty")).toHaveCount(0);
      await geometry(page);
      await page.screenshot({ path: info.outputPath(`unreachable-${theme}.png`), fullPage: true });
      await callout.getByRole("button", { name: "Try again" }).click();
      await expect.poll(() => writes.refreshes.length).toBe(2);
    });
  });
}

test("the automatic check waits for the CSRF token instead of being refused (#1440)", async ({ page }) => {
  const writes = await setup(page);
  // A slow first config read: the gallery view mounts long before the token is known.
  await page.route("**/api/config", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.fallback();
  });
  await page.goto(GALLERY_SKILLS_PATH);
  await expect(page.getByRole("heading", { name: "Review checklist" })).toBeVisible();
  await expect.poll(() => writes.refreshes.length).toBe(1);
  await expect(page.getByTestId("gallery-refused")).toHaveCount(0);
  await expect(page.getByTestId("gallery-unreachable")).toHaveCount(0);
});

test("without a session token the check gives up plainly after its bound, and Try again recovers (#1440)", async ({ page }) => {
  const writes = await setup(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/config", async (route) => {
    await held;
    await route.fallback();
  });
  await page.goto(GALLERY_SKILLS_PATH);
  // No hang on "Checking…": after the bound it reads as can't-be-reached, and nothing was POSTed.
  await expect(page.getByTestId("gallery-unreachable")).toBeVisible({ timeout: 15000 });
  expect(writes.refreshes).toHaveLength(0);
  release();
  await page.getByTestId("gallery-unreachable").getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("gallery-unreachable")).toHaveCount(0);
  await expect.poll(() => writes.refreshes.length).toBe(1);
});

test("a listing that fails verification says nothing changed", async ({ page }) => {
  await setup(page, { refresh: "refused" });
  await page.goto(GALLERY_SKILLS_PATH);
  await expect(page.getByTestId("gallery-refused")).toContainText(
    "The public gallery sent a listing that couldn't be verified, so nothing changed.",
  );
  // What was stored before stays listed.
  await expect(page.getByRole("heading", { name: "Review checklist" })).toBeVisible();
});

// The menu's entries are pinned by SectionNav.test.tsx (no Gallery entry since #1440).
test("Playbooks and Skills each hold Your … and Browse gallery", async ({ page }) => {
  await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  await expect(page.getByRole("link", { name: "Your playbooks" })).toHaveAttribute("aria-current", "page");
  await page.getByRole("link", { name: "Browse gallery" }).click();
  await expect(page).toHaveURL(new RegExp(`${GALLERY_PLAYBOOKS_PATH}$`));
  await page.goto(SKILLS_PATH);
  await expect(page.getByRole("link", { name: "Your skills" })).toHaveAttribute("aria-current", "page");
  await page.getByRole("link", { name: "Browse gallery" }).click();
  await expect(page).toHaveURL(new RegExp(`${GALLERY_SKILLS_PATH}$`));
});

test.describe("old gallery links land in Playbooks and Skills (#1440)", () => {
  const note = (page: Page) => page.getByTestId("gallery-landing-note");
  test("the bare link opens the playbook gallery with a dismissible note", async ({ page }) => {
    await setup(page);
    await page.goto(GALLERY_PATH);
    await expect(page).toHaveURL(new RegExp(`${GALLERY_PLAYBOOKS_PATH}$`));
    await expect(note(page)).toHaveText("The gallery now lives in Playbooks and Skills.");
    await note(page).getByRole("button", { name: "Dismiss" }).click();
    await expect(note(page)).toHaveCount(0);
  });
  for (const [kind, target, title] of [
    ["skill", digest, "Review checklist"],
    ["playbook", playbookDigest, "Inbox triage"],
  ] as const)
    test(`an entry link opens that ${kind} in its own view`, async ({ page }) => {
      await setup(page, { playbooks: true });
      await page.goto(`${GALLERY_PATH}/${target}`);
      await expect(page).toHaveURL(
        new RegExp(`${kind === "skill" ? GALLERY_SKILLS_PATH : GALLERY_PLAYBOOKS_PATH}/${target}$`),
      );
      await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
      await expect(note(page)).toHaveText("The gallery now lives in Playbooks and Skills.");
    });
  for (const link of [
    `${GALLERY_PATH}/${"f".repeat(64)}`,
    `${GALLERY_PATH}/not-a-digest`,
    `${GALLERY_PATH}?kind=widget&id=x`,
    `${GALLERY_PATH}?kind=__proto__&id=x`,
  ])
    test(`${link} lands on Browse gallery with a plain note`, async ({ page }) => {
      await setup(page);
      await page.goto(link);
      await expect(page).toHaveURL(new RegExp(`${GALLERY_PLAYBOOKS_PATH}$`));
      await expect(note(page)).toHaveText("That gallery link doesn't point to anything any more.");
      expect(page.url()).not.toContain("widget");
    });
  test("a compare link for a missing local skill lands on Your skills", async ({ page }) => {
    await setup(page);
    await page.goto(`${GALLERY_PATH}?kind=skill&id=missing-skill`);
    await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}$`));
    await expect(note(page)).toHaveText("No local skill called “missing-skill”.");
  });
  test("a compare link for a missing local playbook lands on Your playbooks", async ({ page }) => {
    await setup(page);
    await page.goto(`${GALLERY_PATH}?kind=playbook&id=missing-playbook`);
    await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}$`));
    await expect(note(page)).toHaveText("No local playbook called “missing-playbook”.");
  });
  test("an invalid local id is never echoed back", async ({ page }) => {
    await setup(page);
    await page.goto(`${GALLERY_PATH}?kind=skill&id=${encodeURIComponent("<b>x</b>")}`);
    await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}$`));
    await expect(note(page)).toHaveText("That gallery link doesn't point to anything any more.");
  });
  test("a compare link with a newer version opens Review update", async ({ page }) => {
    await setup(page, { newer: true });
    await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
    await expect(page).toHaveURL(new RegExp(`${GALLERY_SKILLS_PATH}/${digest}\\?update=my-skill$`));
    await expect(page.getByRole("heading", { name: "Review update" })).toBeVisible();
  });
  test("a compare link without a newer version opens the local copy", async ({ page }) => {
    await setup(page);
    await page.goto(`${GALLERY_PATH}?kind=skill&id=my-skill`);
    await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}/my-skill$`));
    await expect(note(page)).toHaveText("The gallery now lives in Playbooks and Skills.");
  });
});

test("a local copy shows Update available only for a newer accepted version", async ({ page }) => {
  await setup(page, { newer: true });
  await page.goto(`${SKILLS_PATH}/my-skill`);
  const source = page.getByRole("region", { name: "Gallery source" });
  await expect(source).toContainText("From the gallery · v1.0.0 · unchanged");
  await expect(source.getByTestId("update-available")).toBeVisible();
  await expect(source.getByRole("link", { name: "Review update to v2.0.0" })).toHaveAttribute(
    "href",
    `${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`,
  );
  // Evidence is folded away.
  await expect(source.getByText(origin.pointer)).toHaveCount(0);
  await expect(source.getByText(digest)).toBeHidden();
});

test("an up-to-date local copy shows no badge", async ({ page }) => {
  await setup(page);
  await page.goto(`${SKILLS_PATH}/my-skill`);
  const source = page.getByRole("region", { name: "Gallery source" });
  await expect(source).toContainText("From the gallery · v1.0.0 · unchanged");
  await expect(source.getByTestId("update-available")).toHaveCount(0);
});

test.describe("a listing kept from an earlier check is shown but never acted on while the current check fails (#1440)", () => {
  const WAIT = "The public gallery can't be reached right now, so this waits until it can.";
  test("the copy shows no Update available", async ({ page }) => {
    await setup(page, { newer: true, refresh: "unreachable" });
    await page.goto(`${SKILLS_PATH}/my-skill`);
    const source = page.getByRole("region", { name: "Gallery source" });
    await expect(source).toContainText("Can't check for updates right now.");
    await expect(source.getByTestId("update-available")).toHaveCount(0);
    await expect(source.getByRole("link", { name: /Review update/ })).toHaveCount(0);
  });
  test("the entry stays viewable but Add waits", async ({ page }) => {
    await setup(page, { refresh: "unreachable" });
    await page.goto(GALLERY_SKILLS_PATH);
    await expect(page.getByTestId("gallery-unreachable")).toContainText("this is the list from the last check");
    await page.getByRole("link", { name: "View Review checklist" }).click();
    const add = page.getByRole("button", { name: "Add to my skills" });
    await expect(add).toBeDisabled();
    await expect(add).toHaveAccessibleDescription(WAIT);
  });
  test("an old compare link lands on the copy, not on Review update", async ({ page }) => {
    await setup(page, { newer: true, refresh: "unreachable" });
    await page.goto(`${GALLERY_PATH}?kind=skill&id=my-skill`);
    await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}/my-skill$`));
  });
  test("a direct Review update cannot add", async ({ page }) => {
    const writes = await setup(page, { newer: true, refresh: "unreachable" });
    await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
    await page.getByRole("group", { name: "Versions of SKILL.md" }).getByRole("button", { name: "Gallery version" }).click();
    const add = page.getByRole("button", { name: "Add updated copy" });
    await expect(add).toBeDisabled();
    await expect(add).toHaveAccessibleDescription(WAIT);
    expect(writes.filter((w) => w.path.endsWith("/update"))).toEqual([]);
  });
});

test.describe("a successful check whose listing read then fails is not acted on (#1440)", () => {
  const WAIT = "The gallery listing couldn't be loaded, so this waits until it can.";
  test("the entry waits, then recovers on Try again", async ({ page }) => {
    const writes = await setup(page, { failCatalogAfterRefresh: true });
    await page.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
    const add = page.getByRole("button", { name: "Add to my skills" });
    await expect(page.getByTestId("gallery-load-error")).toBeVisible();
    await expect(add).toBeDisabled();
    await expect(add).toHaveAccessibleDescription(WAIT);
    writes.control.catalogOk = true;
    await page.getByTestId("gallery-load-error").getByRole("button", { name: "Try again" }).click();
    await expect(page.getByTestId("gallery-load-error")).toHaveCount(0);
    await expect(add).toBeEnabled();
  });
  test("the copy shows no Update available", async ({ page }) => {
    await setup(page, { newer: true, failCatalogAfterRefresh: true });
    await page.goto(`${SKILLS_PATH}/my-skill`);
    const source = page.getByRole("region", { name: "Gallery source" });
    await expect(source).toContainText("Can't check for updates right now.");
    await expect(source.getByTestId("update-available")).toHaveCount(0);
  });
});

test.describe("the listing read after a successful check holds writes until it lands (#1440)", () => {
  test("Add waits during the read, then opens", async ({ page }) => {
    const writes = await setup(page, { holdSecondRead: true });
    await page.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
    const add = page.getByRole("button", { name: "Add to my skills" });
    await expect.poll(() => writes.refreshes.length).toBe(1);
    // The refresh succeeded; the listing read after it is held, so the old listing is not acted on.
    await expect(add).toBeDisabled();
    await expect(add).toHaveAccessibleDescription("Checking the public gallery…");
    writes.control.release();
    await expect(add).toBeEnabled();
  });
  test("Update available waits during the read, then shows", async ({ page }) => {
    const writes = await setup(page, { newer: true, holdSecondRead: true });
    await page.goto(`${SKILLS_PATH}/my-skill`);
    const source = page.getByRole("region", { name: "Gallery source" });
    await expect.poll(() => writes.refreshes.length).toBe(1);
    await expect(source).toContainText("Checking for updates…");
    await expect(source.getByTestId("update-available")).toHaveCount(0);
    writes.control.release();
    await expect(source.getByTestId("update-available")).toBeVisible();
  });
});

test("on a phone, Add comes after every file and a long file scrolls in its own viewer", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, { playbooks: true });
  await page.goto(`${GALLERY_PLAYBOOKS_PATH}/${playbookDigest}`);
  const add = page.getByRole("button", { name: "Add to my playbooks" });
  await expect(add).toBeVisible();
  const files = page.getByRole("region", { name: "Files" });
  const [filesBox, addBox, techBox] = [
    await files.boundingBox(),
    await add.boundingBox(),
    await technical(page).boundingBox(),
  ];
  expect(addBox!.y).toBeGreaterThanOrEqual(filesBox!.y + filesBox!.height - 1);
  expect(techBox!.y).toBeGreaterThan(addBox!.y);
  const viewer = files.locator("pre").first();
  const [scrollHeight, clientHeight] = await viewer.evaluate((el) => [el.scrollHeight, el.clientHeight]);
  expect(clientHeight).toBeLessThanOrEqual(420);
  expect(scrollHeight).toBeGreaterThan(clientHeight);
  // The viewer scrolls to the end of the 125-line file by itself.
  await viewer.evaluate((el) => el.scrollTo(0, el.scrollHeight));
  await expect(viewer.getByText('line 125 = "value 125"', { exact: false })).toBeInViewport();
  // Keyboard: every file toggle is reachable and opens.
  const toggle = files.locator("summary").filter({ hasText: "README.md" });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Triage the inbox.", { exact: true })).toBeVisible();
  await geometry(page);
});

test("a late completion in another tab preserves the newer recovery handle", async ({ page, context }) => {
  await setup(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let first = "";
  await page.route("**/api/gallery/import", async (route) => {
    first = route.request().postDataJSON().operation;
    await held;
    await route.fulfill({ json: { state: "complete", operation: first, result: { kind: "skill", id: "first-copy", revision, gallery_origin: origin } } });
  });
  await page.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
  await technical(page).click();
  await page.getByLabel("Local ID").fill("first-copy");
  await page.getByRole("button", { name: "Add to my skills" }).click();
  await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
  await expect.poll(() => first).not.toBe("");
  const other = await context.newPage();
  await setup(other, { lost: true });
  await other.goto(`${GALLERY_SKILLS_PATH}/${digest}`);
  await other.getByRole("button", { name: "Check status" }).click();
  await other.getByRole("button", { name: "Try again" }).click();
  await confirmDialog(other).getByRole("button", { name: "Yes, try again", exact: true }).click();
  await technical(other).click();
  await other.getByLabel("Local ID").fill("second-copy");
  await other.getByRole("button", { name: "Add to my skills" }).click();
  await confirmDialog(other).getByRole("button", { name: "I've reviewed it — Add" }).click();
  const key = "battlelab-gallery-local-operation-v1";
  await expect.poll(() => other.evaluate((name) => localStorage.getItem(name), key)).not.toBe(null);
  const second = await other.evaluate((name) => localStorage.getItem(name), key);
  expect(second).not.toBe(first);
  const response = page.waitForResponse((r) => r.url().endsWith("/api/gallery/import"));
  release();
  await response;
  // Allow the late promise continuation and the recovery panel's 700 ms poll to run.
  await page.waitForTimeout(1000);
  expect(await other.evaluate((name) => localStorage.getItem(name), key)).toBe(second);
  await expect(other.getByLabel("Pending gallery operation")).toContainText(second!);
});

test("an unresolved conflict preselects no version, and the save bar says why until a choice is made (#1417)", async ({ page }) => {
  const writes = await setup(page);
  await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
  const versions = page.getByRole("group", { name: "Versions of SKILL.md" });
  const incoming = versions.getByRole("button", { name: "Gallery version" });
  for (const name of ["Your edit", "Gallery version", "Original"])
    await expect(versions.getByRole("button", { name })).toHaveAttribute("aria-pressed", "false");
  const add = page.getByRole("button", { name: "Add updated copy" });
  await technical(page).click();
  await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("keyboard-copy");
  await expect(add).toBeDisabled();
  await expect(add).toHaveAccessibleDescription("Choose a result for SKILL.md first.");
  expect(writes.filter((w) => w.path.endsWith("/update"))).toEqual([]);
  // A segment is an ordinary button: Space picks it from the keyboard.
  await incoming.focus();
  await page.keyboard.press("Space");
  await expect(incoming).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("Result for SKILL.md (you can edit it)")).toHaveValue("Incoming");
  await expect(add).toBeEnabled();
  await expect(page.getByText("Adds as keyboard-copy.", { exact: true })).toBeVisible();
  await add.click();
  await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
  await expect(page.getByRole("link", { name: "Open", exact: true })).toBeVisible();
  const updates = writes.filter((w) => w.path.endsWith("/update"));
  expect(updates).toHaveLength(1);
  expect(updates[0].body).toMatchObject({ id: "my-skill", new_id: "keyboard-copy", files: { "SKILL.md": "Incoming" } });
});

test("recompare keeps a disappearing retained change visible and requires resolution", async ({ page }) => {
  const writes = await setup(page, { conflict: true, disappear: true });
  await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
  await page.getByLabel("Result for SKILL.md (you can edit it)").fill("Reviewed retained draft");
  const add = page.getByRole("button", { name: "Add updated copy" });
  await add.click();
  const dialog = confirmDialog(page);
  await dialog.getByRole("button", { name: "I've reviewed it — Add" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Local content changed");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Compare again" }).click();
  await expect(page.getByRole("button", { name: "Compare again" })).toBeEnabled();
  // Every version is now "Original", yet the retained draft still needs an explicit decision.
  await expect(page.getByRole("group", { name: "Versions of SKILL.md" })).toBeVisible();
  await expect(page.getByLabel("Result for SKILL.md (you can edit it)")).toHaveValue("Reviewed retained draft");
  await expect(add).toBeDisabled();
  await page.getByRole("button", { name: "Keep my result" }).click();
  await add.click();
  await confirmDialog(page).getByRole("button", { name: "I've reviewed it — Add" }).click();
  await expect(page.getByText("Added the updated copy", { exact: false })).toBeVisible();
  expect(writes.filter(w => w.path.endsWith("/update")).at(-1)?.body.files).toEqual({ ...content.files, "SKILL.md": "Reviewed retained draft" });
});

test("the compare view and its save bar fit a 320px phone (#1417)", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await setup(page);
  await page.goto(`${GALLERY_SKILLS_PATH}/${digest}?update=my-skill`);
  await expect(page.getByRole("heading", { name: "Your edit" })).toBeVisible();
  await page.getByRole("group", { name: "Versions of SKILL.md" }).getByRole("button", { name: "Your edit" }).click();
  await geometry(page);
  // Every element of the review stays inside the pane; the HUD frame's corner brackets are
  // decoration that deliberately sits on the pane's edge.
  const over = await page.locator("main main").evaluate((el) => {
    const edge = el.getBoundingClientRect().right;
    return [...el.querySelectorAll("*")]
      .filter((c) => !c.closest(".hud-cnr") && c.getBoundingClientRect().right > edge + 0.5)
      .map((c) => `${c.tagName}.${c.className}`);
  });
  expect(over).toEqual([]);
  // The pinned bar's two actions sit side by side or stacked, never on top of each other.
  const [a, b] = await Promise.all(
    ["Compare again", "Add updated copy"].map((name) =>
      page.getByRole("button", { name }).boundingBox(),
    ),
  );
  expect(a && b).toBeTruthy();
  const overlap = !(a!.x + a!.width <= b!.x || b!.x + b!.width <= a!.x || a!.y + a!.height <= b!.y || b!.y + b!.height <= a!.y);
  expect(overlap).toBe(false);
  expect(Math.max(a!.x + a!.width, b!.x + b!.width)).toBeLessThanOrEqual(320);
  // The last file's result and the copy ID each come to rest above the pinned bar, even when
  // scrolled to the pane's bottom edge (#1417).
  await technical(page).click();
  const bar = page.getByTestId("gallery-savebar");
  for (const target of [
    page.getByLabel("Result for SKILL.md (you can edit it)"),
    page.getByRole("textbox", { name: "New copy ID", exact: true }),
  ]) {
    await target.evaluate((el) => el.scrollIntoView({ block: "end" }));
    const [box, pinned] = [await target.boundingBox(), await bar.boundingBox()];
    expect(box!.y + box!.height).toBeLessThanOrEqual(pinned!.y + 1);
  }
  await expect(page.getByRole("button", { name: "Add updated copy" })).toBeEnabled();
});
