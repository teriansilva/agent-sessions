import { expect, test, type Page } from "@playwright/test";

/** The GIT tab's WRITE side (#806) — real-browser proof, desktop AND mobile.
 *
 *  What is asserted here is deliberately the set jsdom cannot establish: that the branch menu
 *  lands inside the viewport and hands focus back, that a coarse-pointer target is reachable at
 *  its **boundary** (a 44px assertion can pass while the tap routes to the neighbouring control —
 *  the failure mode #782 already recorded), that the panel does not scroll horizontally at 360px,
 *  and that the status letters and the amber primary clear a contrast floor on BOTH grounds.
 *
 *  Every write is mocked at the network edge, so this pins the panel's behaviour rather than
 *  git's — `tests/test_gitwrite.py` is where the git semantics are proven against real repos.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const UUID = "aaaaaaaa-0000-4000-8000-000000000001";

const SESSION = {
  id: `claude:${UUID}`,
  engine: "claude",
  title: "git write session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, name: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

const entry = (over: Record<string, unknown>) => ({
  path: "x",
  index: ".",
  worktree: "M",
  kind: "changed",
  oid: "aaa",
  ...over,
});

const DIRTY = {
  repo: CWD,
  branch: "devopsagent/git-write",
  upstream: "origin/devopsagent/git-write",
  ahead: 1,
  behind: 2,
  truncated: false,
  entries: [
    entry({ path: "src/files.py", index: "M", worktree: ".", kind: "staged" }),
    entry({ path: "web/src/GitTab.tsx" }),
    entry({ path: "notes/new.md", index: "?", worktree: "?", kind: "untracked", oid: null }),
  ],
};

const CLEAN = { ...DIRTY, ahead: 0, behind: 0, entries: [] };

const BRANCHES = {
  repo: CWD,
  current: "devopsagent/git-write",
  local: ["devopsagent/git-write", "main", "spike"],
  remote: ["origin/main"],
};

/** Deliberately NOT `origin`: the control must render the target the SERVER resolved, so a
 *  hardcoded `origin` in the UI would show up here as a mismatch. */
const PUSH_TARGET = {
  ok: true,
  reason: null,
  branch: "devopsagent/git-write",
  remote: "upstream",
  target: "upstream/devopsagent/git-write",
  // The opaque expectation the POST must echo back: it pins the destination the preflight
  // resolved, not just its label. Deliberately different from `target` so a client that sends
  // the label instead would be visible here.
  expect: "upstream/devopsagent/git-write@0123456789abcdef",
  candidates: ["upstream", "origin"],
  set_upstream: true,
};

type Opts = {
  status?: unknown;
  push?: unknown;
  onWrite?: (url: string) => void;
  /** Park the push preflight until this settles, to observe the pre-answer state. */
  pushParked?: Promise<void>;
};

async function mockApp(page: Page, opts: Opts = {}) {
  const status = opts.status ?? DIRTY;
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
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        total: 0,
        complete: true,
        truncated: false,
        entries: [],
      },
    }),
  );
  await page.route("**/api/git/status**", (r) => r.fulfill({ json: status }));
  await page.route("**/api/git/branches**", (r) => r.fulfill({ json: BRANCHES }));
  await page.route("**/api/git/push-target**", async (r) => {
    if (opts.pushParked) await opts.pushParked;
    await r.fulfill({ json: opts.push ?? PUSH_TARGET });
  });
  // Every write answers with a post-write status, which is what the panel settles from.
  for (const op of ["stage", "discard", "commit", "push", "fetch", "pull", "switch"]) {
    await page.route(`**/api/git/${op}`, (r) => {
      opts.onWrite?.(r.request().url());
      r.fulfill({ json: { status: CLEAN, discarded: ["web/src/GitTab.tsx"], files: 1 } });
    });
  }
}

async function openGit(page: Page, opts: Opts = {}) {
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
  await page.getByRole("tab", { name: /Git/ }).click();
  await expect(page.locator("[data-git-tab]")).toBeVisible();
}

/** WCAG relative-luminance contrast between two computed `rgb()` strings. */
function contrast(a: string, b: string): number {
  const lum = (c: string) => {
    const [r, g, bl] = (c.match(/[\d.]+/g) ?? ["0", "0", "0"]).slice(0, 3).map(Number);
    const ch = (v: number) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(bl);
  };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/** The painted background behind an element — walks up past transparent ancestors. */
const groundOf = (page: Page, sel: string) =>
  page.locator(sel).first().evaluate((el) => {
    let n: HTMLElement | null = el as HTMLElement;
    while (n) {
      const bg = getComputedStyle(n).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
      n = n.parentElement;
    }
    return "rgb(0, 0, 0)";
  });

test("the branch strip is the menu trigger, and the menu lands inside the viewport", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-branch-trigger]").click();
  const menu = page.locator("[data-branch-menu]");
  await expect(menu).toBeVisible();
  await expect(menu).toContainText("main");
  await expect(menu).toContainText("origin/main");
  // A popover clipped by `.terminal-pane`'s overflow, or pushed off the right edge at 360px, is
  // exactly the class of bug a DOM emulator reports as passing.
  const box = (await menu.boundingBox())!;
  const vp = page.viewportSize()!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vp.width + 1);
  expect(box.y).toBeGreaterThanOrEqual(0);
});

test("the branch menu is keyboard-operable and hands focus back on Escape", async ({ page }) => {
  await openGit(page);
  const trigger = page.locator("[data-branch-trigger]");
  await trigger.click();
  await expect(page.locator("[data-branch-menu]")).toBeVisible();
  await page.keyboard.press("ArrowDown");
  // Focus must be INSIDE the menu, not left on <body> where arrow keys do nothing.
  expect(
    await page.evaluate(() => !!document.activeElement?.closest("[data-branch-menu]")),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-branch-menu]")).toHaveCount(0);
  // Focus return is the assertion; a menu that closes onto <body> strands a keyboard user.
  expect(await page.evaluate(() => document.activeElement?.getAttribute("data-branch-trigger"))).toBe(
    "",
  );
});

test("switching branch from the menu posts a switch and settles from the server", async ({
  page,
}) => {
  const writes: string[] = [];
  await openGit(page, { onWrite: (u) => writes.push(u) });
  await page.locator("[data-branch-trigger]").click();
  await page.locator("[data-branch='main']").click();
  await expect.poll(() => writes.filter((u) => u.endsWith("/switch")).length).toBe(1);
  // The panel shows the SERVER's post-write status, not an optimistic guess.
  await expect(page.locator("[data-git-notice]")).toBeVisible();
  await expect(page.locator("[data-git-row]")).toHaveCount(0);
});

test("discard confirms, naming the repository and the docked session", async ({ page }) => {
  await openGit(page);
  await page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']").click();
  const dialog = page.locator("[data-discard-confirm]");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("proj");
  await expect(dialog).toContainText(`claude:${UUID}`);
  await expect(dialog).toContainText("web/src/GitTab.tsx");
});

test("Escape cancels a discard and returns focus to the control that opened it", async ({
  page,
}) => {
  const writes: string[] = [];
  await openGit(page, { onWrite: (u) => writes.push(u) });
  await page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']").click();
  await expect(page.locator("[data-discard-confirm]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("[data-discard-confirm]")).toHaveCount(0);
  expect(writes.filter((u) => u.endsWith("/discard"))).toHaveLength(0);
  expect(await page.evaluate(() => document.activeElement?.getAttribute("data-git-op"))).toBe(
    "discard",
  );
});

test("an untracked row offers no discard control at all", async ({ page }) => {
  await openGit(page);
  // Absent, not disabled: `git restore` has nothing to restore an untracked file FROM, so this
  // control would be an unrecoverable delete wearing the same glyph.
  await expect(
    page.locator("[data-git-row='notes/new.md'] [data-git-op='discard']"),
  ).toHaveCount(0);
  await expect(page.locator("[data-git-row='notes/new.md'] [data-git-op='stage']")).toHaveCount(1);
});

test("PUSH renders the target the SERVER resolved, not a hardcoded origin", async ({ page }) => {
  await openGit(page);
  const push = page.locator("[data-git-op='push']");
  await expect(push).toContainText("upstream");
  await expect(push).not.toContainText("origin");
  await expect(page.locator("[data-git-tab], .hud-tag")).not.toHaveCount(0);
});

test("an ambiguous push target renders as a refusal naming the candidates", async ({ page }) => {
  await openGit(page, {
    push: {
      ok: false,
      reason:
        "`devopsagent/git-write` has no upstream and this repository has several remotes (origin, backup) — name the one to push to",
      branch: "devopsagent/git-write",
      remote: null,
      target: null,
      candidates: ["origin", "backup"],
      set_upstream: false,
    },
  });
  await expect(page.locator("[data-push-refusal]")).toContainText("several remotes");
  await expect(page.locator("[data-git-op='push']")).toBeDisabled();
});

test("a server refusal renders as prose in the panel, not a vanished toast", async ({ page }) => {
  await openGit(page);
  await page.route("**/api/git/pull", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "`devopsagent/git-write` has diverged from origin — pull is fast-forward only." },
    }),
  );
  await page.locator("[data-git-op='pull']").click();
  const err = page.locator("[data-git-error]");
  await expect(err).toContainText("fast-forward only");
  // Still there a beat later: a refusal the operator can miss is a refusal that did not happen.
  await page.waitForTimeout(1200);
  await expect(err).toContainText("fast-forward only");
});

test("commit is refused with an empty index, and the control says why", async ({ page }) => {
  await openGit(page, { status: CLEAN });
  const commit = page.locator("[data-git-op='commit']");
  await expect(commit).toBeDisabled();
  await expect(commit).toHaveAttribute("title", /Nothing is staged/);
  await expect(page.locator("[data-git-message]")).toBeDisabled();
});

test("a dirty tree blocks a switch, and the menu says so rather than carrying work across", async ({
  page,
}) => {
  const writes: string[] = [];
  await openGit(page, { onWrite: (u) => writes.push(u) });
  await page.route("**/api/git/switch", (r) =>
    r.fulfill({
      status: 409,
      json: { detail: "2 uncommitted changes would follow you onto `main` — commit or discard them first" },
    }),
  );
  await page.locator("[data-branch-trigger]").click();
  await page.locator("[data-branch='main']").click();
  await expect(page.locator("[data-git-error]")).toContainText("would follow you onto");
});

test.describe("coarse pointer", () => {
  test.skip(({ isMobile }) => !isMobile, "target geometry only matters on touch");

  test("the discard glyph is reachable at its BOUNDARY, not just at its centre", async ({
    page,
  }) => {
    await openGit(page);
    const glyph = page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']");
    const box = (await glyph.boundingBox())!;
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    // The assertion that actually matters: TAP the top-left inset of the declared target and
    // require the discard dialog — a 44px box can measure fine while the tap routes to the row
    // underneath it, which is the failure #782 recorded and a box measurement cannot catch.
    await page.touchscreen.tap(box.x + 3, box.y + 3);
    await expect(page.locator("[data-discard-confirm]")).toBeVisible();
    // And the row's own viewer must NOT have opened instead.
    await expect(page.locator("[data-file-viewer]")).toHaveCount(0);
  });

  test("the bottom-right inset of the discard glyph hits the same control", async ({ page }) => {
    await openGit(page);
    const glyph = page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']");
    const box = (await glyph.boundingBox())!;
    await page.touchscreen.tap(box.x + box.width - 3, box.y + box.height - 3);
    await expect(page.locator("[data-discard-confirm]")).toBeVisible();
  });

  test("nothing scrolls horizontally at 360px", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 740 });
    await openGit(page);
    // The panel is the sheet at this width; a horizontal scrollbar inside it means a control
    // (or the commit box) is overflowing, which no jsdom test can see.
    const over = await page.evaluate(() => {
      const el = document.querySelector("[data-git-tab]") as HTMLElement | null;
      const doc = document.documentElement;
      return {
        panel: el ? el.scrollWidth - el.clientWidth : 0,
        page: doc.scrollWidth - doc.clientWidth,
      };
    });
    expect(over.panel).toBeLessThanOrEqual(1);
    expect(over.page).toBeLessThanOrEqual(1);
  });

  test("the branch menu still fits at 360px", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 740 });
    await openGit(page);
    await page.locator("[data-branch-trigger]").tap();
    const box = (await page.locator("[data-branch-menu]").boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(361);
  });
});

for (const theme of ["dark", "light"] as const) {
  test(`the write controls clear a contrast floor on ${theme}`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await openGit(page);
    // The amber primary is the tab's ONE accent surface; if it fails contrast the accent rule is
    // buying nothing. 4.5:1 is the AA floor for this text size.
    const pull = page.locator("[data-git-op='pull']");
    const fg = await pull.evaluate((el) => getComputedStyle(el).color);
    const bg = await groundOf(page, "[data-git-op='pull']");
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5);

    // A status letter is a one-character signal; it is paired with a title, but it still has to
    // be legible on both grounds rather than only on the one the palette was tuned against.
    const letter = page.locator("[data-git-row='web/src/GitTab.tsx'] span").first();
    const lfg = await letter.evaluate((el) => getComputedStyle(el).color);
    const lbg = await groundOf(page, "[data-git-row='web/src/GitTab.tsx']");
    expect(contrast(lfg, lbg)).toBeGreaterThanOrEqual(3);
  });
}

// ---------------------- review round 2 (#825): stale root + push preflight ----------------------

test("PUSH is not clickable until the preflight has named a destination", async ({ page }) => {
  // Before the fix the control was enabled while the preflight was still in flight, so a click
  // pushed with no destination ever shown — the inverse of the issue's contract.
  let release: (() => void) | null = null;
  const parked = new Promise<void>((r) => (release = r));
  const writes: string[] = [];
  await openGit(page, { pushParked: parked, onWrite: (u) => writes.push(u) });
  const push = page.locator("[data-git-op='push']");
  await expect(push).toBeDisabled();
  await expect(push).toHaveAttribute("title", /Working out where/);
  release!();
  await expect(push).toBeEnabled();
  await expect(push).toContainText("upstream");
  expect(writes.filter((u) => u.endsWith("/push"))).toHaveLength(0);
});

test("an ambiguous push offers the candidates, so the refusal is a next step not a dead end", async ({
  page,
}) => {
  // The issue says the client "must resend with an explicit remote from that list". Without a
  // control that path was unreachable and the refusal was terminal.
  const asked: string[] = [];
  await openGit(page, {
    push: {
      ok: false,
      reason: "`devopsagent/git-write` has no upstream and this repository has several remotes (origin, backup)",
      branch: "devopsagent/git-write",
      remote: null,
      target: null,
      candidates: ["origin", "backup"],
      set_upstream: false,
    },
  });
  await expect(page.locator("[data-git-op='push']")).toBeDisabled();
  await page.route("**/api/git/push-target**", (r) => {
    asked.push(r.request().url());
    r.fulfill({
      json: { ...PUSH_TARGET, remote: "backup", target: "backup/devopsagent/git-write" },
    });
  });
  await page.locator("[data-push-candidate='backup']").click();
  await expect(page.locator("[data-git-op='push']")).toBeEnabled();
  await expect(page.locator("[data-git-op='push']")).toContainText("backup");
  expect(asked.some((u) => u.includes("remote=backup"))).toBe(true);
});

test("a write that finishes after the root moved does not overwrite the new root's rows", async ({
  page,
}) => {
  // The data-loss path: rows describing repo A while every control targets repo B's root, so
  // confirming a discard on a shared path destroys the wrong repository's work.
  let releaseFetch: (() => void) | null = null;
  const parked = new Promise<void>((r) => (releaseFetch = r));
  await openGit(page);
  // Repo A's slow fetch answers with a status that names a DIFFERENT repo and a lone row.
  await page.route("**/api/git/fetch", async (r) => {
    await parked;
    await r.fulfill({
      json: {
        remote: "origin",
        status: {
          ...DIRTY,
          repo: "/home/u/OTHER",
          branch: "from-repo-A",
          entries: [entry({ path: "A-ONLY.txt" })],
        },
      },
    });
  });
  await page.locator("[data-git-op='fetch']").click();
  // The operator moves the panel to another folder while that fetch is still in flight.
  await page.route("**/api/git/status**", (r) =>
    r.fulfill({ json: { ...DIRTY, repo: `${CWD}/sub`, branch: "repo-B", entries: [] } }),
  );
  await page.locator("[data-file-panel] [aria-label='Refresh']").click();
  await page.evaluate(() => {
    const crumb = document.querySelector<HTMLElement>("[data-file-panel] [class*='crumbBtn']");
    crumb?.click();
  });
  releaseFetch!();
  await page.waitForTimeout(600);
  // A-ONLY.txt must never appear: it belongs to a repository this panel is no longer showing.
  await expect(page.locator("[data-git-row='A-ONLY.txt']")).toHaveCount(0);
  await expect(page.locator("[data-git-tab]")).not.toContainText("from-repo-A");
});

test("changing root hides the old repo's rows BEFORE the new status arrives", async ({
  page,
}) => {
  // The remaining half of the root-scoping bug, and the half a Refresh click hides.
  //
  // Writes were tagged with their starting root in round 2, but the panel's own status poll was
  // not: `gitRes` carried only a `tick`, and moving the root does not bump the tick. So between
  // navigating A -> B and B's status arriving, `gitLoading` stayed false and repo A's rows stayed
  // on screen — under a `root` that was already B. Every control in the tab targets the current
  // root, so clicking discard on a visible A row sends B plus that path; if B has a file by the
  // same name, the server's fresh-path check passes and B's work is destroyed.
  //
  // Deliberately no Refresh click: Refresh bumps the tick, which makes the stale render resolve
  // itself and would let this test pass against the unfixed code.
  await openGit(page);
  await expect(page.locator("[data-git-row='web/src/GitTab.tsx']")).toBeVisible();

  // B's status never answers while we look, so anything on screen is necessarily A's.
  let releaseStatus: (() => void) | null = null;
  const parked = new Promise<void>((r) => (releaseStatus = r));
  await page.route("**/api/git/status**", async (r) => {
    await parked;
    await r.fulfill({
      json: { ...DIRTY, repo: "/home/u", branch: "repo-B", entries: [] },
    });
  });

  // Navigate to another root the way the operator does — an ancestor crumb, no refresh.
  await page.evaluate(() => {
    const crumb = document.querySelector<HTMLElement>("[data-file-panel] [class*='crumbBtn']");
    crumb?.click();
  });

  // With B's status still in flight, A's rows must already be gone: they describe a repository
  // the panel is no longer pointed at, and every button next to them now aims at B.
  await expect(page.locator("[data-git-row='web/src/GitTab.tsx']")).toHaveCount(0);
  await expect(page.locator("[data-git-row='src/files.py']")).toHaveCount(0);
  // The whole panel, not `[data-git-tab]`: while the new root's status is in flight the tab
  // renders its loading state and that element does not exist, so asserting absence *on* it
  // would pass for the wrong reason (missing element) and keep passing if it came back stale.
  await expect(page.locator("[data-file-panel]")).not.toContainText("devopsagent/git-write");

  releaseStatus!();
  // ...and the new root's own status does land, so this is not just "everything is hidden".
  // The branch strip is pinned outside the scrolling body, hence `[data-file-panel]`.
  await expect(page.locator("[data-file-panel]")).toContainText("repo-B");
});

test("a delayed same-root refresh disables the destructive controls", async ({
  page,
}) => {
  // Root scoping fixed the A->B case; this is the same-ROOT half. A refresh bumps the tick, and
  // until the new status lands the rows on screen describe a state the server may already have
  // moved past. They stay visible (blanking a whole panel on every poll would be worse), but they
  // must not stay ACTIONABLE: the agent in this session edits the same tree, so clicking discard
  // on a row rendered before the refresh can throw away work written after it. The server's
  // fresh-path check does not save us — it confirms the path is still changed, which it is; it
  // cannot know the CONTENT is newer than what the operator was looking at.
  await openGit(page);
  const discard = page.locator("[data-git-op='discard']").first();
  await expect(discard).toBeEnabled();

  // Park the next status so the refresh stays outstanding while we look.
  let release: (() => void) | null = null;
  const parked = new Promise<void>((r) => (release = r));
  await page.route("**/api/git/status**", async (r) => {
    await parked;
    await r.fulfill({ json: DIRTY });
  });

  await page.locator("[data-file-panel] [aria-label='Refresh']").click();

  // Rows are still shown — and every write control is inert until the refresh settles.
  await expect(page.locator("[data-git-row='web/src/GitTab.tsx']")).toBeVisible();
  await expect(discard).toBeDisabled();
  await expect(page.locator("[data-git-op='stage']").first()).toBeDisabled();
  await expect(page.locator("[data-git-op='commit']")).toBeDisabled();

  release!();
  // ...and live again once the panel is showing current state.
  await expect(discard).toBeEnabled();
});

test("the discard confirmation CONTAINS Tab at both boundaries", async ({ page }) => {
  // The dialog claims `aria-modal`, and FilePanel's own sheet trap deliberately stands down while
  // it is open — so if the dialog does not own Tab, nothing does, and focus can reach the panel
  // behind a still-visible destructive confirmation. Escape and focus-return do not cover this:
  // containment is a separate property and needs its own cycle at both ends.
  await openGit(page);
  await page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']").click();
  const dialog = page.locator("[data-discard-confirm]");
  await expect(dialog).toBeVisible();

  const inside = () =>
    page.evaluate(() => !!document.activeElement?.closest("[data-discard-confirm]"));

  // Forward past the last control, and backward before the first: both must wrap, not escape.
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    expect(await inside(), `Tab #${i + 1} left the dialog`).toBe(true);
  }
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Shift+Tab");
    expect(await inside(), `Shift+Tab #${i + 1} left the dialog`).toBe(true);
  }
  // And nothing was discarded along the way.
  await expect(dialog).toBeVisible();
});

test("focus returns to the discard control after CONFIRMING, not only after cancelling", async ({
  page,
}) => {
  await openGit(page);
  await page.locator("[data-git-row='web/src/GitTab.tsx'] [data-git-op='discard']").click();
  await expect(page.locator("[data-discard-confirm]")).toBeVisible();
  await page.locator("[data-discard-go]").click();
  await expect(page.locator("[data-discard-confirm]")).toHaveCount(0);
  // The row settles from the server's post-write status, so the original trigger may be gone —
  // what must not happen is focus being left on <body> with no way back into the panel.
  // POLLED, not sampled once: the dialog closes synchronously but the discard is async, so
  // restoration happens after the post-write status lands. Reading `activeElement` immediately
  // measures the gap rather than the outcome.
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const a = document.activeElement as HTMLElement | null;
          return a?.tagName === "BODY" ? "BODY" : (a?.closest("[data-file-panel]") ? "panel" : "elsewhere");
        }),
      { timeout: 15_000 },
    )
    .toBe("panel");
});
