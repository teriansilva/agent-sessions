import { expect, type Locator, type Page, test, type WebSocketRoute } from "@playwright/test";

/** Files INSIDE the window (#1109 P3) — real-browser proof, desktop only.
 *
 *  What jsdom cannot see: whether the drawer really overlays the pane without moving the
 *  agent's grid (the no-refit invariant), whether the tree is reachable above the terminal's
 *  own layers, whether the editor's save round-trips through the pane's socket-mock, and
 *  whether the dock↔sheet flip at the 620px body boundary keeps the panel alive. The unit
 *  wiring is pinned in `SessionWindowChrome.test.tsx`; this spec pins the behaviour.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";
const PY_PATH = `${CWD}/app.py`;
const PY = 'def greet(name):\n    return "hello " + name\n';
const V1 = "v1-aaaa";
const V2 = "v2-bbbb";

type Write = { path: string; content: string; expect: string };

/** The id the terminal's socket reports when the test types CONVERGE — the fresh-launch
 *  reconcile (#127): the window's ACTION id becomes this mid-life, transport id unchanged. */
const CONVERGED_ID = "claude:cccc0000-0000-4000-8000-00000000000c";
const SESSION2 = {
  id: "claude:bbbb0000-0000-4000-8000-000000000002",
  engine: "claude",
  uuid: "bbbb0000-0000-4000-8000-000000000002",
  short_uuid: "bbbb0000",
  cwd: "/home/u/other",
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: NOW - 90,
  first_user_message: "",
  title: "files window session 2",
  sticky: false,
  archived: false,
  ai_summary: "",
};

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  uuid: "aaaaaaaa-0000-4000-8000-000000000001",
  short_uuid: "aaaaaaaa",
  cwd: CWD,
  project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
  last_mtime: NOW - 60,
  first_user_message: "",
  title: "files window session",
  sticky: false,
  archived: false,
  ai_summary: "",
};

function payload() {
  return {
    sessions: [SESSION, SESSION2],
    next_offset: null,
    total: 1,
    facets: { projects: [], engines: [] },
  };
}

interface SockLog {
  resize: Record<string, number>;
  grids: Record<string, string[]>;
}

declare global {
  interface Window {
    __convergeId?: () => void;
  }
}

async function mockApp(page: Page): Promise<{
  writes: Write[];
  log: SockLog;
  /** Report the reconciled id over the terminal socket (#127 converge), as the server would
   *  after a fresh launch. A page binding rather than typed input: the reconcile has to fire
   *  while the file VIEWER — a workspace-modal that owns the whole screen — is open, and no
   *  keyboard path reaches the terminal through it. */
  converge: () => Promise<void>;
}> {
  const writes: Write[] = [];
  const T0 = Date.now();
  const log: SockLog = { resize: {}, grids: {} };
  let termWs: WebSocketRoute | null = null;
  await page.exposeFunction("__convergeId", () => {
    termWs?.send(JSON.stringify({ t: "id", sid: CONVERGED_ID }));
  });
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }] } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  let sessionsHits = 0;
  await page.route("**/api/sessions?**", (r) => {
    sessionsHits += 1;
    return r.fulfill({ json: payload() });
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
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
          { name: "README.md", path: `${CWD}/README.md`, kind: "file", size: 20, mtime: NOW },
        ],
        total: 2,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/read**", (r) =>
    r.fulfill({
      json: {
        path: new URL(r.request().url()).searchParams.get("path"),
        size: PY.length,
        binary: false,
        content: PY,
        truncated: false,
        version: V1,
        editable: true,
        readonly_reason: null,
        eol: "\n",
        bom: false,
      },
    }),
  );
  await page.route("**/api/files/write", async (r) => {
    const body = r.request().postDataJSON() as Write;
    writes.push(body);
    await r.fulfill({
      status: 200,
      json: {
        path: body.path,
        version: V2,
        size: body.content.length,
        retained: { path: `${CWD}/.previous-app.py`, version: body.expect },
      },
    });
  });
  await page.routeWebSocket(/\/ws\/term\//, (ws: WebSocketRoute) => {
    termWs = ws;
    const key = decodeURIComponent(new URL(ws.url()).pathname.replace("/ws/term/", ""));
    log.resize[key] ??= 0;
    log.grids[key] ??= [];
    ws.onMessage((raw) => {
      if (typeof raw !== "string") return;
      try {
        const msg = JSON.parse(raw) as { t?: string; d?: string };
        if (msg.t === "r") {
          log.resize[key] += 1;
          log.grids[key]?.push(`${Date.now() - T0}:${msg.cols}x${msg.rows}`);
        }
      } catch {
        /* not a control frame we care about */
      }
    });
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[H${"files window spec\r\n".repeat(40)}`));
  });
  return {
    writes,
    log,
    converge: () => page.evaluate(() => window.__convergeId()),
    sessionsHits: () => sessionsHits,
  };
}

const win = (page: Page): Locator =>
  page.locator(`[data-session-window="${SESSION.id}"]`);

/** The Files chip, wherever the fold put it — inline on the bar, or in the ONE ⋯ menu. */
async function openFiles(page: Page): Promise<Locator> {
  const slot = win(page).locator("[data-window-actions-slot]");
  const chip = slot.locator("[data-head-action='files']");
  await expect(chip.or(slot.locator("[data-window-menu]")).first()).toBeVisible();
  if (await chip.isVisible()) {
    await chip.click();
  } else {
    await win(page).locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await menu.getByRole("menuitem", { name: "Browse session files" }).click();
  }
  const drawer = win(page).locator("[data-window-files-drawer]");
  await expect(drawer).toBeVisible();
  return drawer;
}

test.describe("desktop", () => {
  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "windows are desktop-only (#208)");
  });

  test("browse in the window: the drawer docks over the pane and the agent's grid never moves", async ({
    page,
  }) => {
    const { log } = await mockApp(page);
    await page.goto("/overview");
    await expect(page.locator(".tr-ov-chip").first()).toBeVisible();
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    // Let the opening fit settle for real: pass once the connect's own refit has landed
    // (every connect sends at least one frame) AND a 700ms window produced no new one — so
    // the snapshot below is a settled count, not a race against the trailing debounce.
    // (Both sides of a naive expect.poll re-evaluate per attempt — a tautology.)
    await expect(async () => {
      const a = log.resize[SESSION.id] ?? 0;
      expect(a).toBeGreaterThanOrEqual(1);
      await page.waitForTimeout(700);
      expect(log.resize[SESSION.id] ?? 0).toBe(a);
    }).toPass();

    const before = log.resize[SESSION.id] ?? 0;
    const termBoxBefore = await win(page).locator(".xterm-screen").boundingBox();
    const drawer = await openFiles(page);
    // The default window (720px) docks: the body can spare TERM_MIN + MIN_W.
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "dock");
    await expect(drawer.locator("[data-file-row]", { hasText: "app.py" })).toBeVisible();

    // THE INVARIANT: the drawer overlays the pane — the terminal's BOX did not change, so the
    // agent's grid is exactly what it was, and no resize frame rode the drawer's opening.
    // Opening files must never march the agent through refits (#227/#349, by construction).
    await page.waitForTimeout(500);
    const termBoxAfter = await win(page).locator(".xterm-screen").boundingBox();
    expect(termBoxAfter).toEqual(termBoxBefore);
    expect(log.resize[SESSION.id]).toBe(before);
  });

  test("open a file, edit it, SAVE — the round-trip rides the existing editor path", async ({
    page,
  }) => {
    const { writes } = await mockApp(page);
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    await openFiles(page);

    await win(page)
      .locator("[data-file-row]", { hasText: "app.py" })
      .first()
      .click();
    // The editor is the SAME FileViewerModal surface the full-screen pane opens — a
    // workspace-modal (portalled to <body>), reused as-is per the issue's architecture.
    const viewer = page.locator("[data-file-viewer]");
    await expect(viewer).toBeVisible();
    await expect(viewer.locator(".cm-editor .cm-content")).toBeVisible();
    await expect(viewer.locator(".cm-content")).toContainText("def greet(name):");

    // Edit and save — the lease/expect save, untouched. Cursor placement is deterministic:
    // click the FIRST line, wait for the editor to hold focus, End, type.
    await viewer.locator("[data-edit-toggle]").click();
    await viewer.locator(".cm-line").first().click();
    await expect(viewer.locator(".cm-editor")).toHaveClass(/cm-focused/);
    await page.keyboard.press("End");
    await page.keyboard.type("  # from the window");
    await expect(viewer.locator("[data-unsaved]")).toBeVisible();
    await viewer.locator("[data-save]").click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].path).toBe(PY_PATH);
    expect(writes[0].expect).toBe(V1);
    expect(writes[0].content).toBe(
      PY.replace("def greet(name):", "def greet(name):  # from the window"),
    );
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);
  });

  test("crossing the dock↔sheet boundary keeps the drawer (and what is open in it) alive", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    const drawer = await openFiles(page);
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "dock");

    // Shrink past the 620px body boundary: the drawer becomes a sheet covering the window body.
    await win(page).locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "sheet");
    // The SAME panel instance: the tree state (root listing) is not re-fetched from scratch —
    // the rows it had are still the rows it shows.
    await expect(drawer.locator("[data-file-row]", { hasText: "app.py" })).toBeVisible();
    // And the contained sheet is INSIDE the window, not portalled over the workspace: the map
    // toolbar above the window is not covered by the sheet's scrim.
    const sheetBox = await win(page)
      .locator("[data-window-files-drawer='sheet'] [data-file-panel='sheet']")
      .boundingBox();
    const winBox = await win(page).boundingBox();
    expect(sheetBox && winBox).toBeTruthy();
    expect(sheetBox!.x).toBeGreaterThanOrEqual(winBox!.x - 1);
    expect(sheetBox!.y).toBeGreaterThanOrEqual(winBox!.y - 1);
    expect(sheetBox!.y + sheetBox!.height).toBeLessThanOrEqual(winBox!.y + winBox!.height + 1);

    // Growing back re-docks, same panel, no remount flicker of the rows. The sheet stole
    // focus when it opened (it is modal-in-window), so the grip is focused again first.
    await win(page).locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowRight");
    }
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "dock");
    await expect(drawer.locator("[data-file-row]", { hasText: "app.py" })).toBeVisible();

    // The panel's own ✕ closes it (dock mode is not modal, so Esc belongs to the terminal
    // here — the sheet's Esc contract is asserted in the sheet test below).
    await drawer.locator("[aria-label='Close the file panel']").click();
    await expect(win(page).locator("[data-window-files-drawer]")).toHaveCount(0);
  });

  test("the sheet's Esc and scrim close INSIDE the window; the viewer stacks above it", async ({
    page,
  }) => {
    await mockApp(page);
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    const drawer = await openFiles(page);

    // Go straight to sheet mode, then open a file from it: the viewer is the workspace-modal
    // it always is, above the contained sheet.
    await win(page).locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "sheet");
    await drawer.locator("[data-file-row]", { hasText: "app.py" }).first().click();
    const viewer = page.locator("[data-file-viewer]");
    await expect(viewer).toBeVisible();
    // Esc in the VIEWER closes the viewer, not the panel under it (the capture-order contract).
    await page.keyboard.press("Escape");
    await expect(viewer).toBeHidden();
    await expect(drawer).toBeVisible();
    // ...and the next Esc closes the contained sheet itself.
    await page.keyboard.press("Escape");
    await expect(win(page).locator("[data-window-files-drawer]")).toHaveCount(0);
  });

  test("an automatic id reconcile never drops a dirty editor (key is the window, not the id)", async ({
    page,
  }) => {
    // Hermes on #1109, the P1: a fresh launch's `new-` placeholder reconciles to the server's
    // real id MID-LIFE — an AUTOMATIC transition. A panel keyed by the action id would
    // unmount on it, and a dirty editor with it. The mount identity is the WINDOW; the
    // server identity follows as a prop.
    const { writes, converge, sessionsHits } = await mockApp(page);
    await page.route("**/api/files/list**", async (r) => {
      await r.fulfill({
        json: {
          path: CWD,
          parent: "/home/u",
          root: "/home/u",
          entries: [
            { name: "app.py", path: PY_PATH, kind: "file", size: PY.length, mtime: NOW },
            { name: "README.md", path: `${CWD}/README.md`, kind: "file", size: 20, mtime: NOW },
          ],
          total: 2,
          complete: true,
          truncated: false,
        },
      });
    });
    await page.route("**/api/files/write**", async (r) => {
      const body = r.request().postDataJSON() as Write;
      writes.push(body);
      await r.fulfill({
        status: 200,
        json: {
          path: body.path,
          version: V2,
          size: body.content.length,
          retained: { path: `${CWD}/.previous-app.py`, version: body.expect },
        },
      });
    });
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    const drawer = await openFiles(page);
    await drawer.locator("[data-file-row]", { hasText: "app.py" }).first().click();
    const viewer = page.locator("[data-file-viewer]");
    await expect(viewer).toBeVisible();
    await expect(viewer.locator(".cm-editor .cm-content")).toBeVisible();
    // Dirty the editor: toggle edit, first line, End, type — the same deterministic path the
    // save test uses.
    await viewer.locator("[data-edit-toggle]").click();
    await viewer.locator(".cm-line").first().click();
    await expect(viewer.locator(".cm-editor")).toHaveClass(/cm-focused/);
    await page.keyboard.press("End");
    await page.keyboard.type("  # dirty thought");
    await expect(viewer.locator("[data-unsaved]")).toBeVisible();

    // The automatic transition: the socket reports the real id the engine minted — fired
    // through the page binding, with the viewer (and its dirty draft) still open.
    // The reconcile itself is observable server-side: the workspace revalidates the session
    // list the moment the {"t":"id"} frame lands (the reconcile-triggered refetch), so
    // waiting for that hit means waiting for the reconcile to have FINISHED before asserting
    // preservation — not asserting preservation against a transition still in flight.
    const hitsBefore = sessionsHits();
    await converge();
    await expect.poll(() => sessionsHits()).toBeGreaterThan(hitsBefore);

    // The panel NEVER remounted: the viewer is still open, the draft still in it.
    await expect(viewer).toBeVisible();
    await expect(viewer.locator(".cm-content")).toContainText("# dirty thought");
    await expect(viewer.locator("[data-unsaved]")).toBeVisible();

    // And the editor is not a zombie on a dead mount: its SAVE still round-trips through the
    // lease/expect path — loaded version V1 retained, the dirty content written. (The files
    // API carries no session identity by design — single-admin, cwd-scoped — so the wire
    // cannot observe the id; the panel's `sessionKey` follow is pinned at unit level, and
    // the mount-survival + working save here are the user-visible contract.)
    await viewer.locator("[data-save]").click();
    await expect.poll(() => writes.length).toBeGreaterThan(0);
    expect(writes.at(-1)?.content).toContain("# dirty thought");
    expect(writes.at(-1)?.expect).toBe(V1);
    await expect(viewer.locator("[data-unsaved]")).toHaveCount(0);
  });

  test("two windows, two sheets: Escape closes only the ACTIVE window's sheet", async ({
    page,
  }) => {
    // Hermes on #1109, the P2: every contained sheet used to install its own document
    // capture listener, so Escape in one closed BOTH. The keyboard belongs to the window
    // that owns focus; a background sheet is inert.
    await mockApp(page);
    await page.setViewportSize({ width: 1920, height: 1200 });
    await page.goto("/overview");
    const chips = page.locator(".tr-ov-chip");
    await chips.nth(0).click();
    await expect(
      page.locator(`[data-session-window="${SESSION.id}"] .xterm-screen`),
    ).toBeVisible();
    await chips.nth(1).click();
    await expect(
      page.locator(`[data-session-window="${SESSION2.id}"] .xterm-screen`),
    ).toBeVisible();

    // The two windows open at the bottom-right cascade — DISJOINT first: window 1 drags well
    // LEFT (the edge clamps a rightward drag into a no-op there). Overlapping windows would
    // put window 1's sheet over window 2's chrome and the chip below would be unclickable.
    const w1 = page.locator(`[data-session-window="${SESSION.id}"]`);
    const bar1 = (await w1.locator("[data-window-head]").boundingBox())!;
    // Drag by the grip, not the bar's centre: the centre can sit on an action chip
    // (#1329), and a press on a control deliberately does not start a window drag.
    await page.mouse.move(bar1.x + 8, bar1.y + bar1.height / 2);
    await page.mouse.down();
    await page.mouse.move(Math.max(40, bar1.x - 900), bar1.y - 200, { steps: 8 });
    await page.mouse.up();

    // Both windows narrow enough to be in SHEET mode, both drawers open.
    for (const id of [SESSION.id, SESSION2.id]) {
      const w = page.locator(`[data-session-window="${id}"]`);
      await w.locator("[data-window-resize]").focus();
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press("Shift+ArrowLeft");
      }
      const slot = w.locator("[data-window-actions-slot]");
      const chip = slot.locator("[data-head-action='files']");
      if (await chip.isVisible()) {
        await chip.click();
      } else {
        await w.locator("[data-window-menu]").click();
        const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
        await menu.getByRole("menuitem", { name: "Browse session files" }).click();
      }
      await expect(w.locator("[data-window-files-drawer]")).toHaveAttribute(
        "data-window-files-drawer",
        "sheet",
      );
    }
    const drawer1 = page.locator(
      `[data-session-window="${SESSION.id}"] [data-window-files-drawer]`,
    );
    const drawer2 = page.locator(
      `[data-session-window="${SESSION2.id}"] [data-window-files-drawer]`,
    );
    await expect(drawer1).toBeVisible();
    await expect(drawer2).toBeVisible();

    // Focus lands inside window 2's sheet — it is the ACTIVE window now. The click target is
    // the already-selected Files tab: it claims focus WITHOUT opening anything (a file row
    // would open the viewer, and the viewer — not the sheet — would own the first Escape).
    await drawer2.getByRole("tab", { name: "Files" }).click();

    // One Escape: only the active window's sheet closes. The background sheet survives.
    await page.keyboard.press("Escape");
    await expect(drawer2).toHaveCount(0);
    await expect(drawer1).toBeVisible();

    // ...and the survivor still closes on its own Escape once IT becomes active.
    await drawer1.getByRole("tab", { name: "Files" }).click();
    await page.keyboard.press("Escape");
    await expect(drawer1).toHaveCount(0);
  });

  test("a session dialog above the sheet owns Escape: the brief closes, the drawer survives", async ({
    page,
  }) => {
    // Hermes on #1109, the last P2: the chrome's ⋯ opens the Session brief OVER an open
    // sheet, and the sheet's capture listener used to eat the Escape first — the FILES
    // drawer closed while the brief on top stayed. The contract is structural now: any
    // workspace-level aria-modal dialog owns the keys while it is open.
    await mockApp(page);
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    await expect(win(page).locator(".xterm-screen")).toBeVisible();
    const drawer = await openFiles(page);
    await win(page).locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "sheet");

    // The Session brief, from the chrome's ⋯ — a workspace-modal above the contained sheet.
    await win(page).locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await menu.getByRole("menuitem", { name: "Session brief" }).click();
    const brief = page.locator("[role='dialog'][aria-modal='true']", {
      hasText: "Session brief",
    });
    await expect(brief).toBeVisible();

    // One Escape: ONLY the brief goes — the sheet under it stays exactly as it was.
    await page.keyboard.press("Escape");
    await expect(brief).toHaveCount(0);
    await expect(drawer).toBeVisible();
    await expect(drawer).toHaveAttribute("data-window-files-drawer", "sheet");

    // And the next Escape is the sheet's own again.
    await page.keyboard.press("Escape");
    await expect(win(page).locator("[data-window-files-drawer]")).toHaveCount(0);
  });
});
