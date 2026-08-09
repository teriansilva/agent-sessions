import { expect, test, type Page } from "@playwright/test";

/** GIT tab (#784) — real-browser proof, desktop AND mobile.
 *
 * The assertions that matter are the ones jsdom cannot make: that a conflict sorts above
 * everything else on screen, that a mode which does not apply is ABSENT rather than a dead
 * control, that the diff's own body scrolls instead of the page, and that the tab is reachable by
 * tap above the terminal's capture layer.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  title: "git tab session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, label: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

const STATUS = {
  repo: CWD,
  branch: "devopsagent/git-tab",
  upstream: "origin/devopsagent/git-tab",
  ahead: 2,
  behind: 0,
  truncated: false,
  entries: [
    {
      path: "src/merge.py",
      index: "U",
      worktree: "U",
      kind: "unmerged",
      oid: null,
    },
    {
      path: "src/files.py",
      index: "M",
      worktree: ".",
      kind: "staged",
      oid: "aaa",
    },
    {
      path: "web/src/GitTab.tsx",
      index: ".",
      worktree: "M",
      kind: "changed",
      oid: "bbb",
    },
    {
      path: "web/src/legacy/Old.tsx",
      index: ".",
      worktree: "D",
      kind: "changed",
      oid: "ccc",
    },
    {
      path: "notes/new.md",
      index: "?",
      worktree: "?",
      kind: "untracked",
      oid: null,
    },
  ],
};

const LONG = Array.from(
  { length: 60 },
  (_, i) => ` a very long unchanged line number ${i} ${"x".repeat(90)}`,
);
const DIFF = [
  "@@ -1,4 +1,5 @@",
  " const a = 1;",
  "-const b = 2;",
  "+const b = 3;",
  "+const c = 4;",
  ...LONG,
].join("\n");

const CONFLICT_DIFF = ["@@ -1 +1 @@", "-ours", "+theirs"].join("\n");

async function mockApp(page: Page) {
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
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
        total: 1,
        complete: true,
        truncated: false,
        entries: [
          {
            name: "src",
            path: `${CWD}/src`,
            kind: "dir",
            size: 4096,
            mtime: NOW,
          },
        ],
      },
    }),
  );
  await page.route("**/api/files/read**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        size: 10,
        binary: false,
        content: "content mode\n",
        truncated: false,
      },
    }),
  );
  await page.route("**/api/git/status**", (r) => r.fulfill({ json: STATUS }));
  await page.route("**/api/git/diff**", (r) => {
    // A conflict diff is stage 2 vs stage 3, and the server says so — the viewer must not label
    // it working-tree/index just because conflict rows open with staged=false.
    const conflict = decodeURIComponent(r.request().url()).includes("merge.py");
    r.fulfill({
      json: {
        path: "x",
        repo: CWD,
        diff: conflict ? CONFLICT_DIFF : DIFF,
        added: conflict ? 1 : 2,
        removed: conflict ? 1 : 1,
        truncated: false,
        binary: false,
        too_large: false,
        conflict,
      },
    });
  });
}

async function openGit(page: Page) {
  await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) await direct.click();
  else {
    await page.getByRole("button", { name: "More session actions" }).click();
    await page.getByRole("menuitem", { name: /Files/ }).click();
  }
  await expect(page.locator("[data-file-panel]")).toBeVisible();
  await page.getByRole("tab", { name: /Git/ }).click();
  await expect(page.locator("[data-git-tab]")).toBeVisible();
}

test("groups render conflict-first, with branch and divergence", async ({
  page,
}) => {
  await openGit(page);
  await expect(page.locator("[data-git-tab]")).toContainText(
    "devopsagent/git-tab",
  );
  await expect(page.locator("[data-git-tab]")).toContainText("AHEAD 2");
  // A conflict blocks everything else, so it must be the first row on screen — an ordering claim
  // that only means anything against real layout.
  const first = page.locator("[data-git-row]").first();
  await expect(first).toHaveAttribute("data-kind", "unmerged");
  await expect(page.locator("[data-git-row]")).toHaveCount(5);
});

test("an untracked row offers CONTENT only — no dead DIFF control", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='notes/new.md']").click();
  await expect(page.locator("[data-file-viewer]")).toBeVisible();
  // Absent, not disabled: a control you can press to no effect is worse than no control.
  await expect(
    page.getByRole("button", { name: "Diff", exact: true }),
  ).toHaveCount(0);
});

test("a deleted row offers DIFF only — there is nothing left to read", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='web/src/legacy/Old.tsx']").click();
  await expect(page.locator("[data-file-viewer]")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Content", exact: true }),
  ).toHaveCount(0);
});

test("a modified row opens the diff with both gutters and switches to content", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='web/src/GitTab.tsx']").click();
  const viewer = page.locator("[data-file-viewer]");
  await expect(viewer).toBeVisible();
  await expect(page.locator("[data-file-diff]")).toBeVisible();
  await expect(viewer).toContainText("const b = 3;");
  await expect(viewer).toContainText("+2 −1");
  await page.getByRole("button", { name: "Content", exact: true }).click();
  await expect(viewer).toContainText("content mode");
});

test("a long diff scrolls inside its own body, never the page", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='web/src/GitTab.tsx']").click();
  await expect(page.locator("[data-file-diff]")).toBeVisible();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("not-a-repository is a state, not a disappearing tab", async ({
  page,
}) => {
  await mockApp(page);
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
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) await direct.click();
  else {
    await page.getByRole("button", { name: "More session actions" }).click();
    await page.getByRole("menuitem", { name: /Files/ }).click();
  }
  await page.getByRole("tab", { name: /Git/ }).click();
  await expect(page.locator("[data-git-tab]")).toHaveCount(0);
  await expect(page.locator("[data-file-panel]")).toContainText(
    "not inside a git working tree",
  );
});

test.describe("touch", () => {
  test.skip(({ isMobile }) => !isMobile, "touch-only");

  test("the GIT tab is tappable above the terminal's capture layer, with 44px rows", async ({
    page,
  }) => {
    await openGit(page);
    const row = page.locator("[data-git-row]").first();
    const box = await row.boundingBox();
    expect(
      box!.height,
      "git rows must be real touch targets",
    ).toBeGreaterThanOrEqual(44);
    const hit = await page.evaluate(
      ({ x, y }) =>
        document.elementFromPoint(x, y)?.closest("[data-git-row]") !== null,
      { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 },
    );
    expect(hit).toBe(true);
    await row.tap();
    await expect(page.locator("[data-file-viewer]")).toBeVisible();
  });
});

test("a conflict diff names the two sides it actually compares", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='src/merge.py']").click();
  const modal = page.locator("[data-file-viewer]");
  await expect(modal).toBeVisible();
  // A conflict opens on CONTENT by design (the markers are what you resolve against), so DIFF is
  // the deliberate second step rather than the default.
  await modal.getByRole("button", { name: "Diff", exact: true }).click();

  // The banner and the footer have to agree with the bytes: ours vs theirs, not working tree vs
  // index. The footer was the one that lied — it selected purely on `staged`.
  await expect(modal.getByText(/Diff \/\/ Conflict/)).toBeVisible();
  await expect(
    modal.getByText(/OURS \(STAGE 2\) vs THEIRS \(STAGE 3\)/),
  ).toBeVisible();
  await expect(modal.getByText("WORKING TREE vs INDEX")).toHaveCount(0);
});
