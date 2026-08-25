import { expect, test, type Page } from "@playwright/test";

/** Uploading into the browsed directory (#807) — real-browser proof, desktop AND mobile.
 *
 *  The assertions worth having here are the ones a DOM emulator cannot make: that a **drop onto a
 *  directory row** targets that directory rather than the panel's root, that a real
 *  `<input type=file>` behind the menu actually feeds the queue, that a per-file failure leaves
 *  the rest of the batch alone on screen, that nothing overflows at 360px with long paths in the
 *  queue, and that the coarse-pointer controls are reachable at their boundaries.
 *
 *  Drag-and-drop is dispatched with a real `DataTransfer` built in the page and a real `drop`
 *  event on the real element under real layout. Playwright has no OS-level file drag, so this is
 *  the strongest form available — and it is still a genuine browser doing genuine hit-testing,
 *  unlike a jsdom event aimed at a component.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const UUID = "aaaaaaaa-0000-4000-8000-000000000001";

const SESSION = {
  id: `claude:${UUID}`,
  engine: "claude",
  title: "upload session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, label: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

const LISTING = {
  path: CWD,
  parent: "/home/u",
  root: "/home/u",
  total: 2,
  complete: true,
  truncated: false,
  entries: [
    { name: "src", path: `${CWD}/src`, kind: "dir", size: 4096, mtime: NOW },
    { name: "README.md", path: `${CWD}/README.md`, kind: "file", size: 12, mtime: NOW },
  ],
};

type Seen = { dir: string; relpath: string; collision: string; batch: string };
type Opts = {
  /** Fail the upload of these relpaths with the given status + detail. */
  fail?: Record<string, { status: number; detail: string }>;
  onUpload?: (s: Seen) => void;
  /** Make the SETTLEMENT of a Skip fail — the transient half of the collision flow. */
  skipFails?: boolean;
};

async function mockApp(page: Page, opts: Opts = {}) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        hostname: "t",
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
  await page.route("**/api/files/list**", (r) => r.fulfill({ json: LISTING }));
  await page.route("**/api/git/status**", (r) =>
    r.fulfill({
      json: {
        repo: null,
        branch: null,
        upstream: null,
        ahead: null,
        behind: null,
        entries: [],
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/upload/batch", (r) =>
    r.fulfill({
      json: {
        batch_id: "b1",
        files: 1,
        bytes: 1,
        files_limit: 500,
        bytes_limit: 262144000,
        file_limit: 26214400,
      },
    }),
  );
  await page.route("**/api/files/upload", async (r) => {
    // The multipart body is what this route is about, so it is parsed rather than assumed:
    // asserting the DIR the panel chose is the whole point of the row-drop test.
    const raw = r.request().postData() ?? "";
    const pick = (name: string) => {
      const m = raw.match(new RegExp(`name="${name}"\\r?\\n\\r?\\n([\\s\\S]*?)\\r?\\n--`));
      return m ? m[1] : "";
    };
    const seen: Seen = {
      dir: pick("dir"),
      relpath: pick("relpath"),
      collision: pick("on_collision"),
      batch: pick("batch_id"),
    };
    opts.onUpload?.(seen);
    const bad = opts.fail?.[seen.relpath];
    if (bad) {
      await r.fulfill({ status: bad.status, json: { detail: bad.detail } });
      return;
    }
    await r.fulfill({
      json: {
        path: `${seen.dir}/${seen.relpath}`,
        name: seen.relpath.split("/").pop(),
        relpath: seen.relpath,
        bytes: 4,
        batch: {
          batch_id: "b1",
          files_used: 1,
          bytes_used: 4,
          files_limit: 500,
          bytes_limit: 262144000,
        },
      },
    });
  });
  // Registered AFTER `/api/files/upload` — Playwright gives the later route precedence, so the
  // settlement call can never be swallowed by the upload handler.
  await page.route("**/api/files/upload/skip", (r) =>
    opts.skipFails
      ? r.fulfill({ status: 503, json: { detail: "could not record the skip" } })
      : r.fulfill({ json: { skipped: true } }),
  );
}

async function openPanel(page: Page, opts: Opts = {}) {
  await mockApp(page, opts);
  await page.goto(`/s/claude/${UUID}`);
  await page.locator("[data-head-action]").first().waitFor();
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) await direct.click();
  else {
    await page.getByRole("button", { name: "More session actions" }).click();
    await page.getByRole("menuitem", { name: /Files/ }).click();
  }
  await expect(page.locator("[data-file-panel]")).toBeVisible();
  await expect(page.locator("[data-file-tree]")).toBeVisible();
}

/** Pick files through the REAL `<input type=file>` behind the Upload menu. */
async function pickFiles(
  page: Page,
  files: { name: string; mimeType: string; buffer: Buffer }[],
) {
  await page.locator("[data-upload-trigger]").click();
  await expect(page.locator("[data-upload-menu]")).toBeVisible();
  await page.locator("[data-upload-pick='files']").click();
  await page.locator("[data-upload-files]").setInputFiles(files);
}

const oneFile = (name: string, bytes = 4) => ({
  name,
  mimeType: "text/plain",
  buffer: Buffer.alloc(bytes, 0x61),
});

/** Dispatch a real `drop` (with a real DataTransfer) on the element matching `selector`. */
async function dropOnto(page: Page, selector: string, names: string[]) {
  await page.locator(selector).first().waitFor();
  await page.evaluate(
    ([sel, list]) => {
      const el = document.querySelector(sel as string);
      if (!el) throw new Error(`no element for ${sel}`);
      const dt = new DataTransfer();
      for (const n of list as string[]) {
        dt.items.add(new File(["abcd"], n, { type: "text/plain" }));
      }
      // dragover first: without it the panel never marks itself a drop target, which is the
      // behaviour a user would hit as "the browser navigated away and opened my file".
      el.dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }));
      el.dispatchEvent(
      new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }),
    );
    el.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
    },
    [selector, names] as const,
  );
}

// --------------------------------------------------------------------------- the picker

test("picking files uploads them into the browsed folder and reports each one", async ({ page }) => {
  const seen: Seen[] = [];
  await openPanel(page, { onUpload: (s) => seen.push(s) });
  await pickFiles(page, [oneFile("notes.md"), oneFile("data.csv")]);
  await expect(page.locator("[data-upload-queue]")).toBeVisible();
  await expect(page.locator("[data-upload-row='notes.md'] [data-upload-state='done']")).toBeVisible();
  await expect(page.locator("[data-upload-row='data.csv'] [data-upload-state='done']")).toBeVisible();
  expect(seen.map((s) => s.dir)).toEqual([CWD, CWD]);
  expect(seen.every((s) => s.batch === "b1")).toBe(true);
});

test("the fields are sent BEFORE the file part, which the route depends on", async ({ page }) => {
  // The server opens the destination when the file part starts, so `dir`/`relpath` must already
  // have been parsed. A FormData reordering would break uploads with a 422 that looks unrelated.
  await openPanel(page);
  const body = page.waitForRequest((r) => r.url().endsWith("/api/files/upload"));
  await pickFiles(page, [oneFile("notes.md")]);
  const raw = (await body).postData() ?? "";
  expect(raw.indexOf('name="dir"')).toBeLessThan(raw.indexOf('name="file"'));
  expect(raw.indexOf('name="relpath"')).toBeLessThan(raw.indexOf('name="file"'));
});

// --------------------------------------------------------------------------- drops

test("a drop onto a DIRECTORY row targets that directory, not the panel root", async ({ page }) => {
  // The decision that shapes the whole feature: dropping into `src` must not require navigating
  // there first. Only a real browser can tell you which element the pointer was actually over.
  const seen: Seen[] = [];
  await openPanel(page, { onUpload: (s) => seen.push(s) });
  await dropOnto(page, "[data-file-row][data-kind='dir']", ["dropped.txt"]);
  await expect(page.locator("[data-upload-row='dropped.txt']")).toBeVisible();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0].dir).toBe(`${CWD}/src`);
});

test("a drop on the tree background lands in the folder being browsed", async ({ page }) => {
  const seen: Seen[] = [];
  await openPanel(page, { onUpload: (s) => seen.push(s) });
  await dropOnto(page, "[data-file-tree]", ["root.txt"]);
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0].dir).toBe(CWD);
});

test("dragging over a directory row marks THAT row as the target", async ({ page }) => {
  await openPanel(page);
  await page.locator("[data-file-row][data-kind='dir']").first().waitFor();
  await page.evaluate(() => {
    const el = document.querySelector("[data-file-row][data-kind='dir']")!;
    const dt = new DataTransfer();
    dt.items.add(new File(["a"], "x.txt"));
    el.dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }));
  });
  await expect(page.locator("[data-file-row][data-drop-target]")).toHaveCount(1);
  await expect(page.locator("[data-file-tree]")).toHaveAttribute("data-drop-dir", `${CWD}/src`);
});

// --------------------------------------------------------------------------- per-file outcomes

test("one failed file is one red row — the rest of the batch still lands", async ({ page }) => {
  await openPanel(page, {
    fail: { "big.bin": { status: 413, detail: "that file is larger than the limit (25 MB)" } },
  });
  await pickFiles(page, [oneFile("a.txt"), oneFile("big.bin"), oneFile("b.txt")]);
  await expect(page.locator("[data-upload-row='a.txt'] [data-upload-state='done']")).toBeVisible();
  await expect(page.locator("[data-upload-row='b.txt'] [data-upload-state='done']")).toBeVisible();
  const bad = page.locator("[data-upload-row='big.bin'] [data-upload-state='failed']");
  await expect(bad).toBeVisible();
  await expect(bad).toContainText("25 MB");
  // The summary states what did not land rather than collapsing to one outcome.
  await expect(page.locator("[data-upload-queue]")).toContainText("NOT LANDED");
});

test("a name collision is an operator CHOICE — nothing is overwritten silently", async ({ page }) => {
  const seen: Seen[] = [];
  await openPanel(page, {
    fail: { "a.txt": { status: 409, detail: "'a.txt' already exists here" } },
    onUpload: (s) => seen.push(s),
  });
  await pickFiles(page, [oneFile("a.txt")]);
  const prompt = page.locator("[data-collision-prompt]");
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText("Nothing has been overwritten");
  // Every retry so far went out with the default, never `replace`.
  expect(seen.every((s) => s.collision === "fail")).toBe(true);
  await page.locator("[data-collision='skip']").click();
  await expect(page.locator("[data-upload-row='a.txt'] [data-upload-state='skipped']")).toBeVisible();
  expect(seen.some((s) => s.collision === "replace")).toBe(false);
});

test("a Skip the SERVER did not accept is a failed row, not a skipped one", async ({ page }) => {
  // The client swallowed a failed settlement and rendered SKIPPED anyway. That is the worst of
  // both: the manifest entry stays pending server-side and holds the batch's slot for the full
  // idle TTL — the exact lockout the endpoint was added to prevent — while the operator is told
  // it was handled. An outcome the server never recorded must not be reported as one.
  await openPanel(page, {
    fail: { "a.txt": { status: 409, detail: "'a.txt' already exists here" } },
    skipFails: true,
  });
  await pickFiles(page, [oneFile("a.txt")]);
  await expect(page.locator("[data-collision-prompt]")).toBeVisible();
  await page.locator("[data-collision='skip']").click();

  const row = page.locator("[data-upload-row='a.txt']");
  await expect(row.locator("[data-upload-state='failed']")).toBeVisible();
  await expect(row.locator("[data-upload-state='skipped']")).toHaveCount(0);
});

test("choosing Replace re-sends that ONE file with the operator's choice", async ({ page }) => {
  const seen: Seen[] = [];
  let first = true;
  await openPanel(page, { onUpload: (s) => seen.push(s) });
  await page.route("**/api/files/upload", async (r) => {
    const raw = r.request().postData() ?? "";
    const collision = raw.match(/name="on_collision"\r?\n\r?\n([\s\S]*?)\r?\n--/)?.[1] ?? "";
    seen.push({ dir: "", relpath: "a.txt", collision, batch: "" });
    if (first && collision === "fail") {
      first = false;
      await r.fulfill({ status: 409, json: { detail: "'a.txt' already exists here" } });
      return;
    }
    await r.fulfill({
      json: { path: "p", name: "a.txt", relpath: "a.txt", bytes: 4, batch: null },
    });
  });
  await pickFiles(page, [oneFile("a.txt")]);
  await expect(page.locator("[data-collision-prompt]")).toBeVisible();
  await page.locator("[data-collision='replace']").click();
  await expect(page.locator("[data-upload-row='a.txt'] [data-upload-state='done']")).toBeVisible();
  expect(seen.map((s) => s.collision)).toContain("replace");
});

test("Escape on the collision prompt skips the file rather than wedging the batch", async ({
  page,
}) => {
  await openPanel(page, {
    fail: { "a.txt": { status: 409, detail: "'a.txt' already exists here" } },
  });
  await pickFiles(page, [oneFile("a.txt")]);
  await expect(page.locator("[data-collision-prompt]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-collision-prompt]")).toHaveCount(0);
  await expect(page.locator("[data-upload-row='a.txt'] [data-upload-state='skipped']")).toBeVisible();
  // The panel itself must still be open — Escape belonged to the prompt.
  await expect(page.locator("[data-file-panel]")).toBeVisible();
});

// --------------------------------------------------------------------------- honesty

test("the footer no longer claims READ ONLY, and says what is still unavailable", async ({
  page,
}) => {
  await openPanel(page);
  const panel = page.locator("[data-file-panel]");
  await expect(panel).not.toContainText("READ ONLY");
  await expect(panel).toContainText("NO MOVE / RENAME / DELETE / EDIT");
});

test("a browser with no webkitdirectory says so instead of offering a broken Folder…", async ({
  browser,
}) => {
  // iOS Safari's actual shape. Simulated by removing the property before the app loads, which is
  // the only honest way to test a capability this engine happens to have.
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.addInitScript(() => {
    const proto = HTMLInputElement.prototype as unknown as Record<string, unknown>;
    delete proto.webkitdirectory;
  });
  await openPanel(page);
  await page.locator("[data-upload-trigger]").click();
  await expect(page.locator("[data-upload-pick='folder']")).toBeDisabled();
  await expect(page.locator("[data-folder-unsupported]")).toContainText("cannot pick a folder");
  // And Files… is still offered — the degradation is a narrowing, not a dead menu.
  await expect(page.locator("[data-upload-pick='files']")).toBeEnabled();
  await ctx.close();
});

// --------------------------------------------------------------------------- layout

test.describe("coarse pointer", () => {
  test.skip(({ isMobile }) => !isMobile, "target geometry only matters on touch");

  test("the queue does not scroll horizontally at 360px, even with long paths", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 740 });
    await openPanel(page);
    await pickFiles(page, [oneFile("a-really-quite-long-name-for-one-file.tsx")]);
    await expect(page.locator("[data-upload-queue]")).toBeVisible();
    const over = await page.evaluate(() => {
      const q = document.querySelector("[data-upload-queue]") as HTMLElement | null;
      const doc = document.documentElement;
      return {
        queue: q ? q.scrollWidth - q.clientWidth : 0,
        page: doc.scrollWidth - doc.clientWidth,
      };
    });
    expect(over.queue).toBeLessThanOrEqual(1);
    expect(over.page).toBeLessThanOrEqual(1);
  });

  test("the collision choices are reachable at their boundaries, not just their centres", async ({
    page,
  }) => {
    await openPanel(page, {
      fail: { "a.txt": { status: 409, detail: "'a.txt' already exists here" } },
    });
    await pickFiles(page, [oneFile("a.txt")]);
    const skip = page.locator("[data-collision='skip']");
    await expect(skip).toBeVisible();
    const box = (await skip.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    // Tap the inset corner: a 44px box can measure fine while the tap routes to the neighbouring
    // control, which is the failure #782 recorded and a box measurement cannot catch.
    await page.touchscreen.tap(box.x + 3, box.y + 3);
    await expect(page.locator("[data-collision-prompt]")).toHaveCount(0);
    await expect(page.locator("[data-upload-row='a.txt'] [data-upload-state='skipped']")).toBeVisible();
  });

  test("the Upload control is a real 44px target", async ({ page }) => {
    await openPanel(page);
    const box = (await page.locator("[data-upload-trigger]").boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    await page.touchscreen.tap(box.x + box.width - 3, box.y + box.height - 3);
    await expect(page.locator("[data-upload-menu]")).toBeVisible();
  });
});

for (const theme of ["dark", "light"] as const) {
  test(`queue states clear a contrast floor on ${theme}`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await openPanel(page, {
      fail: { "bad.bin": { status: 413, detail: "that file is larger than the limit (25 MB)" } },
    });
    await pickFiles(page, [oneFile("good.txt"), oneFile("bad.bin")]);
    await expect(page.locator("[data-upload-state='failed']")).toBeVisible();
    const contrast = (fg: string, bg: string) => {
      const lum = (c: string) => {
        const parts = (c.match(/[\d.]+/g) ?? ["0", "0", "0"]).slice(0, 3).map(Number);
        // A `color-mix()` result serializes as `color(srgb 0.82 0.48 0.49)` — FRACTIONAL
        // channels, not 0-255. Reading those as 8-bit values makes every mixed colour compute
        // as near-black, which reports a passing colour as 1.14:1 (measured: `--git-del-fg`).
        // `light-readability.spec.ts` already records this serialization quirk.
        const scale = c.startsWith("color(") ? 255 : 1;
        const [r, g, b] = parts.map((n) => n * scale);
        const ch = (v: number) => {
          const s = v / 255;
          return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        };
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
      };
      const [x, y] = [lum(fg), lum(bg)].sort((p, q) => q - p);
      return (x + 0.05) / (y + 0.05);
    };
    for (const state of ["done", "failed"]) {
      const el = page.locator(`[data-upload-state='${state}']`).first();
      const fg = await el.evaluate((n) => getComputedStyle(n).color);
      const bg = await el.evaluate((n) => {
        let p: HTMLElement | null = n as HTMLElement;
        while (p) {
          const c = getComputedStyle(p).backgroundColor;
          if (c && c !== "rgba(0, 0, 0, 0)" && c !== "transparent") return c;
          p = p.parentElement;
        }
        return "rgb(0, 0, 0)";
      });
      // A per-file outcome is the thing the queue exists to communicate; it has to be legible
      // on both grounds, not only on the one the palette was tuned against.
      expect(contrast(fg, bg), `${state} on ${theme}`).toBeGreaterThanOrEqual(3);
    }
  });
}

// ---------------------- review round 2 (#827): concurrency ----------------------

test("three concurrent collisions are asked ONE AT A TIME and the batch settles", async ({
  page,
}) => {
  // With three workers, concurrent 409s each overwrote a single resolver: the operator answered
  // the one prompt they could see and the other two waited forever, so the queue never settled.
  await openPanel(page, {
    fail: {
      "a.txt": { status: 409, detail: "'a.txt' already exists here" },
      "b.txt": { status: 409, detail: "'b.txt' already exists here" },
      "c.txt": { status: 409, detail: "'c.txt' already exists here" },
    },
  });
  await pickFiles(page, [oneFile("a.txt"), oneFile("b.txt"), oneFile("c.txt")]);
  for (let i = 0; i < 3; i++) {
    await expect(page.locator("[data-collision-prompt]")).toBeVisible({ timeout: 15_000 });
    // Exactly one prompt at a time — three stacked dialogs would be its own bug.
    await expect(page.locator("[data-collision-prompt]")).toHaveCount(1);
    await page.locator("[data-collision='skip']").click();
  }
  await expect(page.locator("[data-collision-prompt]")).toHaveCount(0);
  // Settled: the header leaves the UPLOADING state, which it never did before the fix.
  await expect(page.locator("[data-upload-queue]")).not.toContainText("UPLOADING", {
    timeout: 20_000,
  });
  await expect(page.locator("[data-upload-state='skipped']")).toHaveCount(3);
});

test("'apply to the rest' answers the queued collisions without asking again", async ({ page }) => {
  await openPanel(page, {
    fail: {
      "a.txt": { status: 409, detail: "'a.txt' already exists here" },
      "b.txt": { status: 409, detail: "'b.txt' already exists here" },
    },
  });
  await pickFiles(page, [oneFile("a.txt"), oneFile("b.txt")]);
  await expect(page.locator("[data-collision-prompt]")).toBeVisible({ timeout: 15_000 });
  await page.locator("[data-apply-rest]").check();
  await page.locator("[data-collision='skip']").click();
  await expect(page.locator("[data-collision-prompt]")).toHaveCount(0);
  await expect(page.locator("[data-upload-state='skipped']")).toHaveCount(2, { timeout: 20_000 });
});

test("a drop during a running batch is REFUSED OUT LOUD, not silently swallowed", async ({
  page,
}) => {
  // This test previously asserted the opposite — that the dropped file simply disappeared — and
  // that assertion was wrong: it pinned silent data loss as correct behaviour. The fence is
  // right (a second `start()` would replace the running batch's rows and sticky collision
  // choice); swallowing the operator's chosen files without a word is not.
  let release: (() => void) | null = null;
  const parked = new Promise<void>((r) => (release = r));
  await openPanel(page);
  await page.route("**/api/files/upload", async (r) => {
    await parked;
    await r.fulfill({
      json: { path: "p", name: "slow.txt", relpath: "slow.txt", bytes: 4, batch: null },
    });
  });
  await pickFiles(page, [oneFile("slow.txt")]);
  await expect(page.locator("[data-upload-row='slow.txt']")).toBeVisible();

  await dropOnto(page, "[data-file-tree]", ["intruder.txt"]);
  // The refusal names what happened and what to do about it.
  await expect(page.locator("[data-upload-refusal]")).toContainText("already running", {
    timeout: 10_000,
  });
  release!();
  await expect(page.locator("[data-upload-row='slow.txt'] [data-upload-state='done']")).toBeVisible(
    { timeout: 20_000 },
  );
});

test("a folder the browser cannot read becomes a visible refusal, not silence", async ({ page }) => {
  // `readEntries` rejects, the drop handler's promise is discarded by the caller, and the
  // rejection went nowhere: no queue, no refusal, no row — the drop produced nothing at all.
  await openPanel(page);
  await page.evaluate(() => {
    const el = document.querySelector("[data-file-tree]")!;
    const dt = new DataTransfer();
    // A REAL file first, purely so `dataTransfer.types` contains "Files" — the tree ignores a
    // drag that does not advertise files, so a fully synthetic DataTransfer never reaches the
    // handler at all.
    dt.items.add(new File(["x"], "placeholder"));
    // …then the entry list is replaced with one that claims to be a directory and fails to
    // enumerate, which is the case under test.
    Object.defineProperty(dt, "items", {
      value: [
        {
          kind: "file",
          webkitGetAsEntry: () => ({
            isFile: false,
            isDirectory: true,
            name: "unreadable",
            createReader: () => ({
              readEntries: (_ok: unknown, err?: (e: unknown) => void) =>
                err?.(new Error("NotReadableError")),
            }),
          }),
        },
      ],
    });
    el.dispatchEvent(
      new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }),
    );
    el.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
  });
  await expect(page.locator("[data-upload-refusal]")).toContainText("could not be read", {
    timeout: 10_000,
  });
});
