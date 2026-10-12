import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { GALLERY_PATH, SKILLS_PATH } from "../src/lib/routes";

const digest = "a".repeat(64),
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
const origin = {
  state: "verified",
  title: content.title,
  id: content.id,
  version: "1.0.0",
  publisher: content.publisher,
  source: content.source,
  entry_digest: digest,
  base_revision: revision,
  pointer: "d".repeat(32),
};
async function setup(
  page: Page,
  options: {
    state?: string; lost?: boolean; conflict?: boolean; disappear?: boolean; collision?: boolean;
    otherFiles?: Record<string, string>; reservedFiles?: boolean;
  } = {},
) {
  await commonMocks(page);
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let current = {
    id: "my-skill",
    revision,
    files: { ...content.files },
    gallery_origin: origin,
    editable: true,
  };
  let updatedId: string | null = null;
  let editedCopy: typeof current | null = null;
  let collision = options.collision;
  let saved = false,
    conflict = options.conflict;
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
    if (path === "/api/gallery" || path.endsWith("/refresh"))
      return reply({
        state: options.state ?? "ready",
        entries: options.state === "empty" ? [] : [entry],
        sequence: 1,
        digest,
        expires_at: 1792000000,
        error: options.state === "unavailable" ? "Gallery unavailable" : null,
      });
    if (path.includes("/content/"))
      return reply({
        entry,
        importable: options.state !== "stale",
        reason: options.state === "stale" ? "Publication expired" : null,
        cut_digest: digest,
      });
    if (path.endsWith("/import")) {
      saved = true;
      current = { ...current, id: request.postDataJSON().id };
      if (options.lost) return route.abort("failed");
      return reply({
        state: "complete",
        operation: request.postDataJSON().operation,
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
        : reply({ detail: "No operation recorded" }, 404);
    if (path.endsWith("/compare") && options.disappear && current.revision === nextRevision) {
      const reverted = { ...content.files, "SKILL.md": "Original" };
      return reply({
        kind: "skill", id: "my-skill", revision: nextRevision, origin: origin.pointer,
        incoming_digest: digest, comparison: nextRevision, base: reverted, local: reverted,
        incoming: reverted, proposed: reverted, conflicts: [], changed_files: [],
        incoming_version: "2.0.0",
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
      });
    if (path.endsWith("/update")) {
      if (collision) {
        collision = false;
        return reply({ detail: "That local skill ID already exists." }, 409);
      }
      if (conflict) {
        conflict = false;
        current = { ...current, revision: nextRevision };
        return reply({ detail: "Local content changed. Compare again." }, 409);
      }
      saved = true;
      updatedId = request.postDataJSON().new_id;
      if (options.lost) return route.abort("failed");
      return reply({
        state: "complete",
        operation: request.postDataJSON().operation,
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
    return route.fulfill({
      json: path === "/api/library/skills"
        ? { skills: [current, ...(editedCopy ? [editedCopy] : [])] }
        : editedCopy && path.endsWith(`/${editedCopy.id}`) ? editedCopy : current,
    });
  });
  return writes;
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
for (const theme of ["dark", "light"]) {
  test.describe(theme, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript(
        (value) => localStorage.setItem("tr-theme", value),
        theme,
      );
    });
    test("inspect full inert content and explicitly import an editable copy", async ({
      page,
    }, info) => {
      const writes = await setup(page);
      await page.goto(GALLERY_PATH);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(
        page.getByRole("heading", { name: "Review checklist", exact: true }),
      ).toBeVisible();
      await geometry(page);
      await page.getByLabel("Search", { exact: true }).fill("no match");
      await expect(
        page.getByText("No gallery content matches this search."),
      ).toBeVisible();
      await page.getByLabel("Search", { exact: true }).fill("");
      await page
        .getByRole("link", { name: "Inspect complete content" })
        .click();
      await page.getByText("SKILL.md", { exact: true }).click();
      await expect(
        page.getByText("Read the change and report concrete findings.", {
          exact: false,
        }),
      ).toBeVisible();
      expect(await page.locator("main img").count()).toBe(0);
      const submit = page.getByRole("button", { name: "Import editable copy" });
      await expect(submit).toBeDisabled();
      expect(writes).toEqual([]);
      await page.getByLabel("Local copy ID").fill("my-skill");
      await page
        .getByLabel("I reviewed the complete content and requirements.")
        .check();
      await expect(submit).toBeEnabled();
      await geometry(page);
      await page.screenshot({
        path: info.outputPath(`inspect-${theme}.png`),
        fullPage: true,
      });
      await submit.click();
      await expect(
        page.getByText("Imported an editable local copy.", { exact: false }),
      ).toBeVisible();
      expect(writes.filter((w) => w.path.endsWith("/import"))).toHaveLength(1);
      await page
        .getByRole("link", { name: "Open local copy", exact: true })
        .click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toBeVisible();
    });
    test("playbook inspection shows inputs, workflow and complete manifest", async ({
      page,
    }, info) => {
      await setup(page);
      const playbook = {
        ...entry,
        content: {
          ...content,
          kind: "playbook",
          id: "inbox-triage",
          title: "Inbox triage",
          files: {
            "playbook.toml": 'format = 1\n[identity]\nid = "inbox-triage"',
            "flows/triage.toml": 'format = 1\n[[steps]]\nid = "read"',
          },
        },
      };
      await page.route("**/api/gallery**", (route) =>
        route.fulfill({
          json: new URL(route.request().url()).pathname.includes("/content/")
            ? {
                entry: playbook,
                importable: true,
                reason: null,
                cut_digest: digest,
                preview: {
                  inputs: [
                    { name: "inbox_name", label: "Inbox", required: true },
                  ],
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
                },
              }
            : { state: "ready", entries: [playbook], error: null },
        }),
      );
      await page.goto(GALLERY_PATH);
      await page.getByRole("combobox", { name: "Kind", exact: true }).selectOption("playbook");
      await page
        .getByRole("link", { name: "Inspect complete content" })
        .click();
      await expect(
        page.getByRole("heading", { name: "Inputs & workflow" }),
      ).toBeVisible();
      await expect(
        page.getByText("Inbox (inbox_name) · required", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("Read the new messages · claude", { exact: true }),
      ).toBeVisible();
      await page.getByText("playbook.toml", { exact: true }).click();
      await expect(
        page.getByText('id = "inbox-triage"', { exact: false }),
      ).toBeVisible();
      await geometry(page);
      await page.screenshot({
        path: info.outputPath(`playbook-${theme}.png`),
        fullPage: true,
      });
    });
    test("lost response is checked after reload and never replayed", async ({
      page,
    }) => {
      const writes = await setup(page, { lost: true });
      await page.goto(`${GALLERY_PATH}/${digest}`);
      await page
        .getByLabel("I reviewed the complete content and requirements.")
        .check();
      await page.getByRole("button", { name: "Import editable copy" }).click();
      await expect(
        page.getByRole("heading", { name: "Check the previous operation" }),
      ).toBeVisible();
      await page.reload();
      await page
        .getByRole("button", { name: "Check operation status" })
        .click();
      await expect(
        page.getByText("The recorded operation completed.", { exact: false }),
      ).toBeVisible();
      expect(writes.filter((w) => w.path.endsWith("/import"))).toHaveLength(1);
    });
    test("local edits survive CAS conflict, compare and guarded navigation", async ({
      page,
    }, info) => {
      const writes = await setup(page, { conflict: true });
      await page.goto(`${SKILLS_PATH}/my-skill`);
      await page
        .getByLabel("Your draft: SKILL.md")
        .fill("My retained instructions");
      await page
        .getByRole("link", { name: "Back to skills", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Leave this draft?" }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
      await page.getByRole("button", { name: "Save edited copy" }).click();
      await expect(page.getByRole("alert")).toContainText("This skill changed");
      await page.getByRole("button", { name: "Check operation status" }).click();
      page.once("dialog", dialog => dialog.accept());
      await page.getByRole("button", { name: "Close checked attempt" }).click();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue(
        "My retained instructions",
      );
      await page
        .getByRole("button", { name: "Compare stored version" })
        .click();
      await expect(
        page.getByText("Changed by another editor", { exact: true }),
      ).toBeVisible();
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue(
        "My retained instructions",
      );
      await geometry(page);
      await page.screenshot({
        path: info.outputPath(`edit-conflict-${theme}.png`),
        fullPage: true,
      });
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
      await expect(page.getByRole("alert")).toContainText("This skill changed");
      await page.getByRole("button", { name: "Check operation status" }).click();
      page.once("dialog", dialog => dialog.accept());
      await page.getByRole("button", { name: "Close checked attempt" }).click();
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
      await expect(page.getByRole("alert")).toBeVisible();
      await page.reload();
      await page.getByRole("button", { name: "Check operation status" }).click();
      await expect(page.getByRole("link", { name: "Open local copy", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/my-skill-edited`);
      expect(writes.filter(w => w.path.endsWith("/copies"))).toHaveLength(1);
      await expect(page.getByLabel("Your draft: SKILL.md")).toHaveValue(content.files["SKILL.md"]);
    });
    test("gallery update creates a separately named copy", async ({ page }) => {
      const writes = await setup(page);
      await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
      await expect(page.getByText("Your original and existing project references stay unchanged.", { exact: false })).toBeVisible();
      const id = page.getByRole("textbox", { name: "New copy ID", exact: true });
      const save = page.getByRole("button", { name: "Create updated copy", exact: true });
      await page.getByLabel("Result for SKILL.md").fill("Reviewed copy");
      await id.fill("my-skill");
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await expect(save).toBeDisabled();
      await id.fill("my-reviewed-skill");
      await expect(save).toBeDisabled();
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await save.click();
      await expect(page.getByRole("link", { name: "Open new copy", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/my-reviewed-skill`);
      await expect(save).toBeDisabled();
      expect(writes.filter(w => w.path.endsWith("/update"))).toHaveLength(1);
      expect(writes.find(w => w.path.endsWith("/update"))?.body).toMatchObject({ id: "my-skill", new_id: "my-reviewed-skill" });
      await page.getByRole("link", { name: "my-skill", exact: true }).click();
      await expect(page.getByRole("textbox", { name: "Your draft: SKILL.md", exact: true })).toHaveValue(content.files["SKILL.md"]);
    });
    test("updated copy recovers a lost response without creating it twice", async ({ page }) => {
      const writes = await setup(page, { lost: true });
      await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
      await page.getByLabel("Result for SKILL.md").fill("Reviewed copy");
      await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("recovered-copy");
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await page.getByRole("button", { name: "Create updated copy" }).click();
      await expect(page.getByRole("heading", { name: "Check the previous operation" })).toBeVisible();
      await page.reload();
      await page.getByRole("button", { name: "Check operation status" }).click();
      await expect(page.getByRole("link", { name: "Open local copy", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/recovered-copy`);
      expect(writes.filter(w => w.path.endsWith("/update"))).toHaveLength(1);
    });
    test("occupied copy ID retains the reviewed draft", async ({ page }) => {
      const writes = await setup(page, { collision: true });
      await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
      await page.getByLabel("Result for SKILL.md").fill("Retain this result");
      await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("occupied-copy");
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await page.getByRole("button", { name: "Create updated copy" }).click();
      await expect(page.getByRole("alert")).toContainText("already exists");
      await expect(page.getByLabel("Result for SKILL.md")).toHaveValue("Retain this result");
      await page.getByRole("button", { name: "Check operation status" }).click();
      page.once("dialog", d => d.accept());
      await page.getByRole("button", { name: "Close checked attempt" }).click();
      await page.getByRole("textbox", { name: "New copy ID", exact: true }).fill("free-copy");
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await page.getByRole("button", { name: "Create updated copy" }).click();
      await expect(page.getByRole("link", { name: "Open new copy", exact: true })).toHaveAttribute("href", `${SKILLS_PATH}/free-copy`);
      expect(writes.filter(w => w.path.endsWith("/update")).at(-1)?.body.files).toHaveProperty(["SKILL.md"], "Retain this result");
    });
    test("gallery comparison supports reserved-property filenames", async ({ page }) => {
      const writes = await setup(page, { reservedFiles: true });
      await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
      for (const name of ["constructor", "toString", "__proto__"]) {
        const file = page.locator("details").filter({ has: page.locator("summary", { hasText: name }) });
        await expect(file.getByText("File absent", { exact: true })).toHaveCount(2);
        await file.getByRole("button", { name: "Keep local file" }).click();
        await expect(file.getByText("This file will be absent.")).toBeVisible();
        await file.getByRole("button", { name: "Use incoming file" }).click();
        await expect(page.getByRole("textbox", { name: `Result for ${name}`, exact: true })).toHaveValue(`Incoming ${name}`);
      }
      await page.getByLabel("I reviewed these results and want to create a new copy.").check();
      await page.getByRole("button", { name: "Create updated copy" }).click();
      await expect(page.getByText("Created an updated copy.", { exact: false })).toBeVisible();
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
    test("three-way review requires resolved conflicts and retains draft after stale save", async ({
      page,
    }, info) => {
      const writes = await setup(page, { conflict: true });
      await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
      await expect(
        page.getByRole("heading", { name: "Imported base" }),
      ).toBeVisible();
      const save = page.getByRole("button", { name: "Create updated copy" });
      await page
        .getByLabel(
          "I reviewed these results and want to create a new copy.",
        )
        .check();
      await expect(save).toBeDisabled();
      await page.getByLabel("Result for SKILL.md").fill("Reviewed merge");
      await page
        .getByLabel(
          "I reviewed these results and want to create a new copy.",
        )
        .check();
      await save.click();
      await expect(page.getByRole("alert")).toContainText(
        "Local content changed",
      );
      await expect(page.getByLabel("Result for SKILL.md")).toHaveValue(
        "Reviewed merge",
      );
      await page
        .getByRole("button", { name: "Check operation status" })
        .click();
      page.once("dialog", (dialog) => dialog.accept());
      await page.getByRole("button", { name: "Close checked attempt" }).click();
      await page
        .getByRole("button", { name: "Compare current revision again" })
        .click();
      await expect(page.getByLabel("Result for SKILL.md")).toHaveValue(
        "Reviewed merge",
      );
      await page
        .getByLabel("Result for SKILL.md")
        .fill("Reviewed merge after comparison");
      await geometry(page);
      await page.screenshot({
        path: info.outputPath(`compare-${theme}.png`),
        fullPage: true,
      });
      await page
        .getByLabel(
          "I reviewed these results and want to create a new copy.",
        )
        .check();
      await save.click();
      await expect(
        page.getByText("Created an updated copy.", { exact: false }),
      ).toBeVisible();
      expect(
        writes.filter((w) => w.path.endsWith("/update")).at(-1)?.body
          .comparison,
      ).toBe(nextRevision);
    });
    for (const state of ["empty", "stale", "unavailable"])
      test(`${state} gallery keeps local library accessible`, async ({
        page,
      }, info) => {
        await setup(page, { state });
        await page.goto(GALLERY_PATH);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await expect(
          page.getByRole("link", { name: "Local skills", exact: true }),
        ).toBeVisible();
        await expect(
          page.getByText(
            state === "empty"
              ? "No accepted gallery content yet"
              : state === "stale"
                ? "This publication has expired."
                : "Gallery unavailable",
            { exact: false },
          ),
        ).toBeVisible();
        if (state === "stale") {
          await page
            .getByRole("link", { name: "Inspect complete content" })
            .click();
          await page
            .getByLabel("I reviewed the complete content and requirements.")
            .check();
          await expect(
            page.getByRole("button", { name: "Import editable copy" }),
          ).toBeDisabled();
        }
        await page.screenshot({
          path: info.outputPath(`${state}-${theme}.png`),
          fullPage: true,
        });
      });
  });
}

test("recompare keeps a disappearing retained change visible and requires resolution", async ({ page }) => {
  const writes = await setup(page, { conflict: true, disappear: true });
  await page.goto(`${GALLERY_PATH}/${digest}?kind=skill&id=my-skill`);
  await page.getByLabel("Result for SKILL.md").fill("Reviewed retained draft");
  const ack = page.getByLabel("I reviewed these results and want to create a new copy.");
  const save = page.getByRole("button", { name: "Create updated copy" });
  await ack.check();
  await save.click();
  await expect(page.getByRole("alert")).toContainText("Local content changed");
  await page.getByRole("button", { name: "Check operation status" }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Close checked attempt" }).click();
  await page.getByRole("button", { name: "Compare current revision again" }).click();
  await expect(page.getByRole("heading", { name: "Local copy", exact: true }).locator("..").getByText("Original", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Result for SKILL.md")).toHaveValue("Reviewed retained draft");
  await ack.check();
  await expect(save).toBeDisabled();
  await page.getByRole("button", { name: "Keep retained result" }).click();
  await ack.check();
  await save.click();
  await expect(page.getByText("Created an updated copy.", { exact: false })).toBeVisible();
  expect(writes.filter(w => w.path.endsWith("/update")).at(-1)?.body.files).toEqual({ ...content.files, "SKILL.md": "Reviewed retained draft" });
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
  await page.goto(`${GALLERY_PATH}/${digest}`);
  await page.getByLabel("Local copy ID").fill("first-copy");
  await page.getByLabel("I reviewed the complete content and requirements.").check();
  await page.getByRole("button", { name: "Import editable copy" }).click();
  await expect.poll(() => first).not.toBe("");
  const other = await context.newPage();
  await setup(other, { lost: true });
  await other.goto(`${GALLERY_PATH}/${digest}`);
  await other.getByRole("button", { name: "Check operation status" }).click();
  other.once("dialog", (dialog) => dialog.accept());
  await other.getByRole("button", { name: "Close checked attempt" }).click();
  await other.getByLabel("Local copy ID").fill("second-copy");
  await other.getByLabel("I reviewed the complete content and requirements.").check();
  await other.getByRole("button", { name: "Import editable copy" }).click();
  const key = "battlelab-gallery-local-operation-v1";
  const second = await other.evaluate((name) => localStorage.getItem(name), key);
  expect(second).toBeTruthy();
  expect(second).not.toBe(first);
  const response = page.waitForResponse((r) => r.url().endsWith("/api/gallery/import"));
  release();
  await response;
  // Allow the late promise continuation and the recovery panel's 700 ms poll to run.
  await page.waitForTimeout(1000);
  expect(await other.evaluate((name) => localStorage.getItem(name), key)).toBe(second);
  await expect(other.getByLabel("Pending gallery operation")).toContainText(second!);
});
