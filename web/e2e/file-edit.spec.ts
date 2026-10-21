import { expect, test, type Locator, type Page } from "@playwright/test";
import { clickHeadAction, FILES_ACTION } from "./headActions";

/** File editing (#950) — real-browser proof, desktop AND mobile.
 *
 *  What jsdom cannot show and these assert: CodeMirror actually mounting and taking input, its
 *  syntax colours and search panel resolving to BattleLab tokens in dark AND light, Tab leaving the
 *  editor instead of being trapped in a modal, the unsaved-edits question on every way out, and
 *  touch targets that fire when tapped at their edges. The API is mocked; the request the viewer
 *  SENDS is what is asserted.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const PY_PATH = `${CWD}/app.py`;
const BIG_PATH = `${CWD}/big.log`;
const V1 = "1".repeat(64);
const V2 = "2".repeat(64);
const V3 = "3".repeat(64);
const PY = 'def greet(name):\n    return "hi " + name\n';

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-00000000e950",
  engine: "claude",
  title: "editor session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, name: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

type Write = { path: string; content: string; expect: string };
type WriteReply = { status: number; json: Record<string, unknown> };

async function mockApp(
  page: Page,
  opts: {
    onWrite?: (w: Write, n: number) => WriteReply;
    disk?: () => { content: string; version: string };
    /** Runs before read number `n` of an editable file is answered: await to park it, return
     *  "fail" to answer 500. The first read is the one that opens the viewer. */
    readHook?: (n: number) => Promise<"fail" | void> | "fail" | void;
  } = {},
) {
  const writes: Write[] = [];
  let reads = 0;
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        hostname: "test",
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [SESSION],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        entries: [
          { name: "app.py", path: PY_PATH, kind: "file", size: PY.length, mtime: NOW },
          { name: "big.log", path: BIG_PATH, kind: "file", size: 3_000_000, mtime: NOW },
        ],
        total: 2,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/read**", async (r) => {
    const path = new URL(r.request().url()).searchParams.get("path");
    if (path === BIG_PATH) {
      return r.fulfill({
        json: {
          path,
          size: 3_000_000,
          binary: false,
          content: "log line\n",
          truncated: true,
          version: null,
          editable: false,
          readonly_reason:
            "is larger than 1 MiB — only the first 1 MiB was loaded, so a save would cut it short",
          eol: "\n",
          bom: false,
        },
      });
    }
    reads += 1;
    if ((await opts.readHook?.(reads)) === "fail") {
      return r.fulfill({ status: 500, json: { detail: "the disk could not be read just now" } });
    }
    const disk = opts.disk?.() ?? { content: PY, version: V1 };
    return r.fulfill({
      json: {
        path,
        size: disk.content.length,
        binary: false,
        content: disk.content,
        truncated: false,
        version: disk.version,
        editable: true,
        readonly_reason: null,
        eol: "\n",
        bom: false,
      },
    });
  });
  await page.route("**/api/files/write", async (r) => {
    const body = r.request().postDataJSON() as Write;
    writes.push(body);
    const reply = opts.onWrite?.(body, writes.length) ?? {
      status: 200,
      json: {
        path: body.path,
        version: V2,
        size: body.content.length,
        retained: { path: "/home/u/.agent-sessions/edit-recovery/1-a/previous-app.py", version: body.expect },
      },
    };
    await r.fulfill({ status: reply.status, json: reply.json });
  });
  return writes;
}

async function openFile(page: Page, name: string) {
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await expect(page.locator("#root")).toBeVisible();
  // Inline chip, the "More" overflow, or the phone's single Actions menu (#948 P6): the helper
  // reaches FILES wherever the header put it.
  await clickHeadAction(page, FILES_ACTION);
  await page.locator("[data-file-row]", { hasText: name }).first().click();
  const viewer = page.locator("[data-file-viewer]");
  await expect(viewer).toBeVisible();
  // CodeMirror, not the plain fallback: the editor chunk has loaded and mounted.
  await expect(viewer.locator(".cm-editor .cm-content")).toBeVisible();
  return viewer;
}

async function typeAtEndOfFirstLine(page: Page, text: string) {
  const firstLine = page.locator("[data-file-viewer] .cm-line").first();
  await firstLine.click();
  // Keys go to whatever holds focus: type only once the editor does. Whether the text is ACCEPTED
  // is each test's own assertion — one of them types into a read-only editor on purpose.
  await expect(page.locator("[data-file-viewer] .cm-editor")).toHaveClass(/cm-focused/);
  await page.keyboard.press("End");
  await page.keyboard.type(text);
}

const AGENT_TEXT = 'def greet(name):\n    return "hello " + name\n';
const RETAINED = "/home/u/.agent-sessions/edit-recovery/1-a/previous-app.py";

/** The first save is refused because the agent wrote the file; every later save lands. `disk` is
 *  what a read returns once that refusal has happened. */
function conflictScenario(disk = { content: AGENT_TEXT, version: V2 }) {
  let changed = false;
  return {
    disk: () => (changed ? disk : { content: PY, version: V1 }),
    onWrite: (w: Write, n: number): WriteReply => {
      if (n === 1) {
        changed = true;
        return {
          status: 409,
          json: {
            detail:
              "the file changed on disk while you were editing — nothing was saved and your edits are kept",
            reason: "changed",
            version: disk.version,
          },
        };
      }
      return {
        status: 200,
        json: { path: w.path, version: V3, size: w.content.length, retained: null },
      };
    },
  };
}

async function editAndConflict(page: Page, viewer: Locator, mine = "  # mine") {
  await viewer.locator("[data-edit-toggle]").click();
  await typeAtEndOfFirstLine(page, mine);
  await viewer.locator("[data-save]").click();
  await expect(viewer.locator("[data-save-conflict]")).toBeVisible();
}

/** One changed row, `app.py`, so the viewer opens with DIFF and CONTENT — the pair that remounts
 *  the editor. */
async function mockGitRow(page: Page) {
  await page.route("**/api/git/status**", (r) =>
    r.fulfill({
      json: {
        repo: CWD,
        branch: "main",
        upstream: null,
        ahead: null,
        behind: null,
        truncated: false,
        entries: [{ path: "app.py", index: ".", worktree: "M", kind: "changed", oid: "aaa" }],
      },
    }),
  );
  await page.route("**/api/git/diff**", (r) =>
    r.fulfill({
      json: {
        path: PY_PATH,
        repo: CWD,
        diff: "@@ -1 +1 @@\n-old\n+new",
        added: 1,
        removed: 1,
        truncated: false,
        binary: false,
        too_large: false,
        conflict: false,
      },
    }),
  );
  await page.route("**/api/git/branches**", (r) =>
    r.fulfill({ json: { repo: CWD, current: "main", local: ["main"], remote: [] } }),
  );
  await page.route("**/api/git/push-target**", (r) =>
    r.fulfill({
      json: {
        ok: false,
        reason: "no remote",
        branch: "main",
        remote: null,
        target: null,
        expect: null,
        candidates: [],
        set_upstream: false,
      },
    }),
  );
}

async function openGitRow(page: Page, rel: string) {
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await expect(page.locator("#root")).toBeVisible();
  // Inline chip, the "More" overflow, or the phone's single Actions menu (#948 P6): the helper
  // reaches FILES wherever the header put it.
  await clickHeadAction(page, FILES_ACTION);
  await page.getByRole("tab", { name: /Git/ }).click();
  await page.locator(`[data-git-row='${rel}']`).click();
  const viewer = page.locator("[data-file-viewer]");
  await expect(viewer).toBeVisible();
  return viewer;
}

/** Two animation frames: long enough for a settled response to have been committed by React. */
const settle = (page: Page) =>
  page.evaluate(
    () => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))),
  );

/** `#rrggbb` → the `rgb(r, g, b)` form getComputedStyle reports. */
function rgb(hex: string): string {
  const n = parseInt(hex.trim().slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

async function tokenColor(page: Page, name: string): Promise<string> {
  return rgb(
    await page.evaluate(
      (n) => getComputedStyle(document.documentElement).getPropertyValue(n),
      name,
    ),
  );
}

/** The computed colour of the element holding exactly `word` inside the editor. */
async function wordColor(page: Page, word: string): Promise<string | null> {
  return page.evaluate((w) => {
    const spans = Array.from(document.querySelectorAll("[data-file-viewer] .cm-content span"));
    const el = spans.find((s) => s.textContent === w);
    return el ? getComputedStyle(el).color : null;
  }, word);
}

test.describe("file editor", () => {
  test("EDIT → type → UNSAVED → SAVE sends the text bound to the loaded version", async ({
    page,
  }) => {
    const writes = await mockApp(page);
    const viewer = await openFile(page, "app.py");

    // Read mode first: no unsaved marker, no save control, the file is on screen.
    await expect(viewer.locator(".cm-content")).toContainText("def greet(name):");
    await expect(viewer.locator("[data-save]")).toHaveCount(0);

    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "  # edited");
    await expect(viewer.locator("[data-unsaved]")).toBeVisible();

    await viewer.locator("[data-save]").click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].path).toBe(PY_PATH);
    expect(writes[0].expect).toBe(V1);
    expect(writes[0].content).toBe(PY.replace("def greet(name):", "def greet(name):  # edited"));

    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);
    await expect(viewer).toContainText(/SAVED \d/);
    await expect(viewer.locator("[data-open-previous]")).toBeVisible();
  });

  test("a file that cannot be edited says why, and offers no way to edit it", async ({ page }) => {
    await mockApp(page);
    const viewer = await openFile(page, "big.log");
    await expect(viewer.locator("[data-readonly-reason]")).toContainText(
      "big.log is larger than 1 MiB",
    );
    await expect(viewer.locator("[data-edit-toggle]")).toBeDisabled();
  });

  test("changed on disk: edits are kept, and OVERWRITE unlocks only after ON DISK", async ({
    page,
  }) => {
    const agentText = 'def greet(name):\n    return "hello " + name\n';
    let diskChanged = false;
    const writes = await mockApp(page, {
      disk: () => (diskChanged ? { content: agentText, version: V2 } : { content: PY, version: V1 }),
      onWrite: (w, n) => {
        if (n === 1) {
          diskChanged = true;
          return {
            status: 409,
            json: {
              detail:
                "the file changed on disk while you were editing — nothing was saved and your edits are kept",
              reason: "changed",
              version: V2,
            },
          };
        }
        return {
          status: 200,
          json: { path: w.path, version: V3, size: w.content.length, retained: null },
        };
      },
    });
    const viewer = await openFile(page, "app.py");
    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "  # mine");
    await viewer.locator("[data-save]").click();

    const banner = viewer.locator("[data-save-conflict]");
    await expect(banner).toBeVisible();
    await expect(viewer.locator(".cm-content")).toContainText("# mine"); // edits kept
    await expect(banner.locator("[data-overwrite]")).toBeDisabled();

    await banner.locator("[data-conflict-view='disk']").click();
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');
    await expect(banner.locator("[data-overwrite]")).toBeEnabled();

    await banner.locator("[data-overwrite]").click();
    await expect.poll(() => writes.length).toBe(2);
    // Bound to the version that was LOOKED AT, carrying the operator's text — not the disk's.
    expect(writes[1].expect).toBe(V2);
    expect(writes[1].content).toContain("# mine");
    await expect(banner).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toContainText("# mine");
  });

  test("closing with unsaved edits asks, and KEEP EDITING keeps them", async ({ page }) => {
    await mockApp(page);
    const viewer = await openFile(page, "app.py");
    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "  # draft");

    await page.keyboard.press("Escape");
    const ask = page.locator("[data-close-confirm]");
    await expect(ask).toBeVisible();
    await ask.locator("[data-keep-editing]").click();
    await expect(ask).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toContainText("# draft");

    await page.getByRole("button", { name: "Close file viewer" }).click();
    await expect(ask).toBeVisible();
    await ask.locator("[data-discard-close]").click();
    await expect(viewer).toHaveCount(0);
  });
});

test.describe("file editor — desktop", () => {
  test.skip(({ isMobile }) => isMobile, "keyboard + pointer assertions");

  test("Ctrl/Cmd+S saves, and Tab leaves the editor instead of being trapped", async ({ page }) => {
    const writes = await mockApp(page);
    const viewer = await openFile(page, "app.py");
    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "!");
    await page.keyboard.press("ControlOrMeta+s");
    await expect.poll(() => writes.length).toBe(1);

    await viewer.locator(".cm-content").click();
    await page.keyboard.press("Tab");
    const inEditor = await page.evaluate(
      () => document.activeElement?.closest(".cm-content") != null,
    );
    expect(inEditor).toBe(false);
    // …and focus did not escape the dialog either.
    const inViewer = await page.evaluate(
      () => document.activeElement?.closest("[data-file-viewer]") != null,
    );
    expect(inViewer).toBe(true);
  });

  test("syntax colours and the search panel are BattleLab tokens, in dark and light", async ({
    page,
  }) => {
    await mockApp(page);
    await openFile(page, "app.py");

    await expect.poll(() => wordColor(page, "def")).toBe(await tokenColor(page, "--syn-keyword"));
    expect(await wordColor(page, "def")).not.toBe(await tokenColor(page, "--accent"));

    await page.locator("[data-file-viewer] .cm-content").click();
    await page.keyboard.press("ControlOrMeta+f");
    const panel = page.locator("[data-file-viewer] .cm-search");
    await expect(panel).toBeVisible();
    const look = await panel.evaluate((el) => {
      const b = el.querySelector(".cm-button") as HTMLElement;
      const f = el.querySelector(".cm-textfield") as HTMLElement;
      const cb = getComputedStyle(b);
      const cf = getComputedStyle(f);
      return {
        buttonRadius: cb.borderTopLeftRadius,
        buttonImage: cb.backgroundImage,
        buttonFont: cb.fontFamily,
        fieldRadius: cf.borderTopLeftRadius,
        panelBg: getComputedStyle(el.closest(".cm-panels") as HTMLElement).backgroundColor,
      };
    });
    expect(look.buttonRadius).toBe("0px");
    expect(look.fieldRadius).toBe("0px");
    expect(look.buttonImage).toBe("none");
    expect(look.panelBg).toBe(await tokenColor(page, "--bg-1"));
    // Esc closes the SEARCH PANEL, not the viewer.
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(page.locator("[data-file-viewer]")).toBeVisible();

    await page.evaluate(() => {
      document.documentElement.dataset.theme = "light";
    });
    await expect.poll(() => wordColor(page, "def")).toBe(await tokenColor(page, "--syn-keyword"));
    expect(await tokenColor(page, "--syn-keyword")).toBe(rgb("#5b3fb0"));
  });
});

test.describe("file editor — touch", () => {
  test.skip(({ isMobile }) => !isMobile, "touch-only assertions");

  test("read mode raises no keyboard; EDIT and SAVE are real 44px targets at their edges", async ({
    page,
  }) => {
    const writes = await mockApp(page);
    const viewer = await openFile(page, "app.py");
    await expect(viewer.locator(".cm-content")).toHaveAttribute("inputmode", "none");

    const edit = viewer.locator("[data-edit-toggle]");
    const eb = (await edit.boundingBox())!;
    expect(eb.height).toBeGreaterThanOrEqual(44);
    await page.touchscreen.tap(eb.x + eb.width / 2, eb.y + 2); // the top edge, not the centre
    await expect(viewer.locator("[data-edit-done]")).toBeVisible();
    await expect(viewer.locator(".cm-content")).not.toHaveAttribute("inputmode", "none");
    // EDIT hands focus to the editor on the next frame; let that land before typing.
    await expect(viewer.locator(".cm-editor")).toHaveClass(/cm-focused/);

    await typeAtEndOfFirstLine(page, "!");
    // SAVE is disabled until there is an unsaved edit, and a tap on a disabled button tests nothing.
    await expect(viewer.locator(".cm-line").first()).toContainText("!");
    await expect(viewer.locator("[data-unsaved]")).toBeVisible();
    const saveBtn = viewer.locator("[data-save]");
    await expect(saveBtn).toBeEnabled();
    const sb = (await saveBtn.boundingBox())!;
    expect(sb.height).toBeGreaterThanOrEqual(44);
    await page.touchscreen.tap(sb.x + sb.width - 2, sb.y + sb.height - 2); // bottom-right corner
    await expect.poll(() => writes.length).toBe(1);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});

test.describe("file editor — the draft survives every save and conflict path", () => {
  test("after a save, Diff → Content shows the saved text and the next save carries it", async ({
    page,
  }) => {
    const writes = await mockApp(page, {
      onWrite: (w, n) => ({
        status: 200,
        json: { path: w.path, version: n === 1 ? V2 : V3, size: w.content.length, retained: null },
      }),
    });
    await mockGitRow(page);
    const viewer = await openGitRow(page, "app.py");
    await viewer.getByRole("button", { name: "Content", exact: true }).click();
    await expect(viewer.locator(".cm-editor .cm-content")).toBeVisible();
    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "  # SAVED_CHANGE");
    await viewer.locator("[data-save]").click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);

    await viewer.getByRole("button", { name: "Diff", exact: true }).click();
    await expect(viewer.locator("[data-file-diff]")).toBeVisible();
    await viewer.getByRole("button", { name: "Content", exact: true }).click();
    // The remounted editor starts from what was SAVED, not from what the viewer first loaded.
    await expect(viewer.locator(".cm-content")).toContainText("# SAVED_CHANGE");
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);

    await typeAtEndOfFirstLine(page, "  # second");
    await viewer.locator("[data-save]").click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].expect).toBe(V2);
    expect(writes[1].content).toContain("# SAVED_CHANGE");
    expect(writes[1].content).toContain("# second");
  });

  test("OVERWRITE from Mine sends the editor as it is now, including typing after the conflict", async ({
    page,
  }) => {
    const writes = await mockApp(page, conflictScenario());
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');
    await banner.locator("[data-conflict-view='mine']").click();
    await expect(viewer.locator(".cm-content")).toContainText("# mine");
    await typeAtEndOfFirstLine(page, "  # more");

    await banner.locator("[data-overwrite]").click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].expect).toBe(V2);
    expect(writes[1].content).toContain("# mine");
    expect(writes[1].content).toContain("# more");
    await expect(viewer.locator(".cm-content")).toContainText("# more");
  });

  test("choosing Mine while Mine is already shown keeps newer typing", async ({ page }) => {
    await mockApp(page, conflictScenario());
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    await typeAtEndOfFirstLine(page, "  # newer");
    await viewer.locator("[data-save-conflict] [data-conflict-view='mine']").click();
    await expect(viewer.locator(".cm-content")).toContainText("# newer");
  });

  test("typing while ON DISK loads is part of the draft Mine brings back", async ({ page }) => {
    let release!: () => void;
    const parked = new Promise<void>((r) => (release = r));
    let reads = 0;
    await mockApp(page, {
      ...conflictScenario(),
      readHook: async (n) => {
        reads = n;
        if (n === 2) await parked;
      },
    });
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect.poll(() => reads).toBe(2);
    await typeAtEndOfFirstLine(page, "  # while-loading");
    release();
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');
    await banner.locator("[data-conflict-view='mine']").click();
    await expect(viewer.locator(".cm-content")).toContainText("# mine");
    await expect(viewer.locator(".cm-content")).toContainText("# while-loading");
  });

  test("an ON DISK read that finishes after Reload took over does not reopen the conflict", async ({
    page,
  }) => {
    let release!: () => void;
    const parked = new Promise<void>((r) => (release = r));
    let reads = 0;
    await mockApp(page, {
      ...conflictScenario(),
      readHook: async (n) => {
        reads = n;
        if (n === 2) await parked;
      },
    });
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect.poll(() => reads).toBe(2);
    await banner.locator("[data-reload-disk]").click();
    await expect(banner).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');

    const late = page.waitForResponse((r) => r.url().includes("/api/files/read"));
    release();
    await late;
    await settle(page);
    await expect(banner).toHaveCount(0);
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);
  });

  test("a failed read while comparing keeps the conflict, the draft and OVERWRITE", async ({
    page,
  }) => {
    const writes = await mockApp(page, {
      ...conflictScenario(),
      readHook: (n) => (n >= 3 ? "fail" : undefined),
    });
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');

    await banner.locator("[data-conflict-view='disk']").click(); // read 3 fails
    await expect(banner.locator("[data-conflict-error]")).toBeVisible();
    await banner.locator("[data-reload-disk]").click(); // read 4 fails
    await expect(banner.locator("[data-conflict-error]")).toBeVisible();
    await expect(banner.locator("[data-overwrite]")).toBeEnabled();

    await banner.locator("[data-conflict-view='mine']").click();
    await expect(viewer.locator(".cm-content")).toContainText("# mine");
    await banner.locator("[data-overwrite]").click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].expect).toBe(V2);
    expect(writes[1].content).toContain("# mine");
  });

  test("opening a kept version with unsaved edits asks first", async ({ page }) => {
    await mockApp(page, {
      onWrite: () => ({
        status: 409,
        json: {
          detail: "the save could not finish cleanly, so both versions were kept",
          reason: "put_back_failed",
          both: [RETAINED, PY_PATH],
        },
      }),
    });
    const viewer = await openFile(page, "app.py");
    await viewer.locator("[data-edit-toggle]").click();
    await typeAtEndOfFirstLine(page, "  # mine");
    await viewer.locator("[data-save]").click();
    const both = viewer.locator("[data-save-both]");
    await expect(both).toBeVisible();
    await typeAtEndOfFirstLine(page, "  # more");

    const open = both.getByRole("button", { name: /Open previous-app\.py/ });
    await open.click();
    const ask = page.locator("[data-close-confirm]");
    await expect(ask).toBeVisible();
    await ask.locator("[data-keep-editing]").click();
    await expect(ask).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toContainText("# more");

    await open.click();
    await ask.locator("[data-discard-close]").click();
    await expect(page.locator("[data-file-viewer]")).toContainText(RETAINED);
  });

  test("closing while ON DISK matches the loaded text still asks about the draft", async ({
    page,
  }) => {
    // The agent's write put the file back exactly as it was loaded — only the version moved.
    await mockApp(page, conflictScenario({ content: PY, version: V2 }));
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect(banner.locator("[data-overwrite]")).toBeEnabled();
    await expect(viewer.locator(".cm-content")).not.toContainText("# mine");

    await page.getByRole("button", { name: "Close file viewer" }).click();
    const ask = page.locator("[data-close-confirm]");
    await expect(ask).toBeVisible();
    await ask.locator("[data-keep-editing]").click();
    await page.keyboard.press("Escape");
    await expect(ask).toBeVisible();
  });

  test("an ON DISK read still loading when Diff is chosen never makes OVERWRITE send an empty file", async ({
    page,
  }) => {
    let release!: () => void;
    const parked = new Promise<void>((r) => (release = r));
    let reads = 0;
    const writes = await mockApp(page, {
      ...conflictScenario(),
      readHook: async (n) => {
        reads = n;
        if (n === 2) await parked;
      },
    });
    await mockGitRow(page);
    const viewer = await openGitRow(page, "app.py");
    await viewer.getByRole("button", { name: "Content", exact: true }).click();
    await expect(viewer.locator(".cm-editor .cm-content")).toBeVisible();
    await editAndConflict(page, viewer);
    // Mine back to exactly the loaded text: the draft is clean, so only a pending read can hold Diff.
    await typeAtEndOfFirstLine(page, "");
    for (let i = 0; i < "  # mine".length; i++) await page.keyboard.press("Backspace");
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);

    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-conflict-view='disk']").click();
    await expect.poll(() => reads).toBe(2);
    const diffBtn = viewer.getByRole("button", { name: "Diff", exact: true });
    const fenced = !(await diffBtn.isEnabled());
    // The review's path: leave CONTENT while the comparison is out, if the viewer allows it.
    if (!fenced) await diffBtn.click();
    release();
    if (await viewer.locator("[data-file-diff]").count()) {
      await viewer.getByRole("button", { name: "Content", exact: true }).click();
    }
    await expect(banner.locator("[data-overwrite]")).toBeEnabled();
    await banner.locator("[data-overwrite]").click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].content).not.toBe("");
    expect(writes[1].content).toBe(PY);
    expect(fenced, "Diff stays unavailable while ON DISK is loading").toBe(true);
  });

  test("while RELOAD reads the disk the editor takes no typing, so nothing typed is silently replaced", async ({
    page,
  }) => {
    let release!: () => void;
    const parked = new Promise<void>((r) => (release = r));
    let reads = 0;
    await mockApp(page, {
      ...conflictScenario(),
      readHook: async (n) => {
        reads = n;
        if (n === 2) await parked;
      },
    });
    const viewer = await openFile(page, "app.py");
    await editAndConflict(page, viewer);
    const banner = viewer.locator("[data-save-conflict]");
    await banner.locator("[data-reload-disk]").click();
    await expect.poll(() => reads).toBe(2);
    await typeAtEndOfFirstLine(page, "  # during-reload");
    const typedWhilePending = (await viewer.locator(".cm-content").textContent()) ?? "";
    const hinted = await banner.locator("[data-conflict-pending]").isVisible();
    release();
    await expect(banner).toHaveCount(0);
    await expect(viewer.locator(".cm-content")).toContainText('"hello "');
    const after = (await viewer.locator(".cm-content").textContent()) ?? "";
    // Either the typing was refused while the replacement was pending, or it survived it — never
    // accepted on screen and then thrown away when the read landed.
    const lost = typedWhilePending.includes("# during-reload") && !after.includes("# during-reload");
    expect(lost, "text typed while RELOAD was pending disappeared").toBe(false);
    expect(hinted, "the banner says the disk is being read").toBe(true);
  });
});
