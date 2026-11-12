/** Artifacts (#1932 P2) in a real browser, desktop and mobile, over a stateful mock of
 *  `/api/artifacts`: the Library list and its server-side filters (inline on desktop, project /
 *  session / mission behind one button on a phone), a site artifact's page with its sandboxed
 *  preview frame, sharing behind "I checked the files" and revoking, the "public links are off"
 *  state (both from the config and from the server's 409), a session's ARTIFACTS tab, the
 *  activation dialog with its restart notice, and the 44 px floor at ≤800px measured at runtime. */
import { expect, test, type Page, type Route } from "@playwright/test";

import { ARTIFACTS_PATH, artifactPath } from "../src/lib/routes";
import { headAction, FILES_ACTION } from "./headActions";
import { CONFIG, commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { animationsSettled } from "./nightjarShell";

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/orbital-api";
const SESSION_KEY = "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001";
const NO_TOOL_KEY = "gemini:7c1e2a00-5b2d-4c8e-9f31-0a6d2e8b4002";
const SITE_ID = "5449-5592-2107-4372";
const IMG_ID = "8023-1176-4490-6215";
const MD_ID = "9530-6128-1472-3389";

type Mode = "off" | "private" | "public";
type Item = Record<string, unknown> & {
  id: string;
  kind: string;
  visibility: string;
  title: string;
};

function art(
  id: string,
  kind: string,
  title: string,
  entry: string,
  files: number,
  bytes: number,
  extra: Partial<Item> = {},
): Item {
  return {
    id,
    title,
    why: `Why ${title}`,
    kind,
    revision: kind === "site" ? 4 : 1,
    created_at: NOW - 7200,
    updated_at: NOW - 720,
    origin: {
      engine: "claude",
      session_key: SESSION_KEY,
      cwd: CWD,
      project_id: "p-orbital",
      mission_id: null,
    },
    visibility: "private",
    url: `/artifacts/${id}/`,
    public_url: null,
    public_since: null,
    files,
    bytes,
    entry,
    ...extra,
  };
}

interface State {
  items: Item[];
  /** The session's own setting, and what it inherits when it has none. */
  sessionMode: Mode | null;
  inherited: Mode;
  /** Sharing answers 409 public_off (the server runs without a login). */
  publicOff: boolean;
  calls: { method: string; path: string; search: string; body: unknown }[];
}

async function mockArtifacts(
  page: Page,
  start: Partial<State> = {},
): Promise<State> {
  const st: State = {
    items: [
      art(
        SITE_ID,
        "site",
        "Ingest rate-limit report",
        "index.html",
        38,
        2_400_000,
      ),
      art(IMG_ID, "file", "p95 latency at 2k rps", "p95-2k.png", 1, 184_000, {
        updated_at: NOW - 2400,
      }),
      art(
        MD_ID,
        "file",
        "Auth middleware review notes",
        "review.md",
        1,
        14_000,
        {
          updated_at: NOW - 172_800,
          visibility: "public",
          public_url: "/artifacts/p/Vq8Lz3N0cR2yWb6hT1eKpA/",
          public_since: NOW - 600,
        },
      ),
    ],
    sessionMode: null,
    inherited: "off",
    publicOff: false,
    calls: [],
    ...start,
  };
  const view = (i: Item) => ({ ...i });
  await page.route("**/api/artifacts**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData()!) : null;
    st.calls.push({ method, path, search: url.search, body });
    if (method !== "GET" && req.headers()["x-csrf-token"] !== CONFIG.csrf)
      return route.fulfill({ status: 403, json: { detail: "csrf" } });
    if (path === "/api/artifacts" && method === "GET") {
      const q = url.searchParams;
      let items = st.items;
      if (q.get("visibility"))
        items = items.filter((i) => i.visibility === q.get("visibility"));
      if (q.get("kind")) items = items.filter((i) => i.kind === q.get("kind"));
      if (q.get("session"))
        items = items.filter(
          (i) =>
            (i.origin as { session_key: string }).session_key ===
            q.get("session"),
        );
      if (q.get("q"))
        items = items.filter((i) =>
          i.title.toLowerCase().includes(q.get("q")!.toLowerCase()),
        );
      return route.fulfill({
        json: {
          items: items.map(view),
          total: items.length,
          next_cursor: null,
        },
      });
    }
    if (path === "/api/artifacts/settings")
      return route.fulfill({
        json: {
          enabled: true,
          artifacts: st.items.length,
          public: st.items.filter((i) => i.visibility === "public").length,
          bytes: 61_900_000,
          caps: {
            revision_bytes: 209_715_200,
            revision_files: 2000,
            revisions: 20,
            total_bytes: 5_368_709_120,
          },
        },
      });
    if (path === "/api/artifacts/activation") {
      if (method === "PUT") {
        if (body.scope === "session") st.sessionMode = body.mode;
        return route.fulfill({
          json: { scope: body.scope, id: body.id, mode: body.mode },
        });
      }
      const scope = url.searchParams.get("scope");
      if (scope !== "session")
        return route.fulfill({
          json: {
            scope,
            id: url.searchParams.get("id"),
            mode: null,
            enabled: true,
          },
        });
      const eff = st.sessionMode ?? st.inherited;
      return route.fulfill({
        json: {
          scope,
          id: url.searchParams.get("id"),
          mode: st.sessionMode,
          enabled: true,
          effective: {
            mode: eff,
            source: st.sessionMode ? "session" : "default",
          },
        },
      });
    }
    const m = path.match(/^\/api\/artifacts\/([0-9-]+)(\/share)?$/);
    const item = m ? st.items.find((i) => i.id === m[1]) : undefined;
    if (!item)
      return route.fulfill({
        status: 404,
        json: { detail: "no such artifact" },
      });
    if (m![2]) {
      if (method === "POST") {
        if (st.publicOff)
          return route.fulfill({
            status: 409,
            json: {
              detail: "public_off",
              reason: "Public links are off without a login.",
            },
          });
        Object.assign(item, {
          visibility: "public",
          public_url: `/artifacts/p/NEW${item.id.replace(/-/g, "")}xyzab/`,
          public_since: NOW,
        });
      } else {
        Object.assign(item, {
          visibility: "private",
          public_url: null,
          public_since: null,
        });
      }
      return route.fulfill({ json: view(item) });
    }
    if (method === "DELETE") {
      st.items = st.items.filter((i) => i !== item);
      return route.fulfill({ json: { deleted: item.id } });
    }
    return route.fulfill({
      json: {
        ...view(item),
        revisions: [
          { n: 4, created_at: NOW - 720, files: 38, bytes: 2_400_000 },
          { n: 3, created_at: NOW - 2000, files: 36, bytes: 2_300_000 },
        ],
        files: [
          { path: "index.html", size: 18_000, type: "text/html" },
          { path: "img/p95.png", size: 212_000, type: "image/png" },
        ],
      },
    });
  });
  // The hosted bytes: what the frame loads (the real server answers with the sandbox CSP).
  await page.route(/^https?:\/\/[^/]+\/artifacts\/[0-9-]+(@\d+)?\/.*$/, (r) =>
    r.fulfill({
      contentType: "text/html",
      headers: { "Content-Security-Policy": "sandbox allow-scripts" },
      body: "<!doctype html><title>Hosted</title><h1>Hosted page</h1>",
    }),
  );
  return st;
}

async function mockBase(page: Page, authMode = "single-user") {
  await commonMocks(page);
  await mockRoster(page);
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { ...CONFIG, auth_mode: authMode } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({
      json: {
        projects: [
          {
            id: "p-orbital",
            name: "orbital-api",
            color: "",
            folders: [CWD],
            default_folder: CWD,
            archived: false,
            created_at: 0,
            session_count: 1,
          },
        ],
      },
    }),
  );
  await page.route(/\/api\/missions($|\?)/, (r) =>
    r.fulfill({
      json: {
        missions: [],
        total: 0,
        limit: 200,
        offset: 0,
        facets: { projects: [], states: [] },
      },
    }),
  );
}

// ---------------------------------------------------------------------------------- the Library

test("the Library lists artifacts with kind and visibility, and filters through the server", async ({
  page,
}, info) => {
  await mockBase(page);
  const st = await mockArtifacts(page);
  await page.goto(ARTIFACTS_PATH);
  const rows = page.locator("[data-artifact]");
  await expect(rows).toHaveCount(3);
  // The kind tag is the entry's extension (SITE, PNG, MD); PUBLIC is a tag, not a status colour.
  await expect(rows.nth(0).locator("[data-kind]").first()).toHaveText("Site");
  await expect(rows.nth(1).locator("[data-kind]").first()).toHaveText("PNG");
  await expect(
    page.locator("[data-artifact] [data-visibility='public']:visible"),
  ).toHaveCount(1);
  await expect(page.getByTestId("artifacts-totals")).toContainText(
    "public links",
  );

  // Visibility → Public: the SERVER filters (the request carries it) and the URL keeps it.
  await page
    .getByRole("radiogroup", { name: "Visibility" })
    .getByRole("radio", { name: "Public" })
    .click();
  await expect(page).toHaveURL(/visibility=public/);
  await expect(rows).toHaveCount(1);
  expect(
    st.calls.some(
      (c) =>
        c.path === "/api/artifacts" && c.search.includes("visibility=public"),
    ),
  ).toBe(true);
  await expect(page.getByTestId("artifacts-active-filters")).toContainText(
    "public",
  );

  // Search, debounced into the URL and the request.
  await page.getByRole("searchbox", { name: "Search artifacts" }).fill("auth");
  await expect(page).toHaveURL(/q=auth/);
  await expect
    .poll(() => st.calls.some((c) => c.search.includes("q=auth")))
    .toBe(true);

  // Project, session and mission: inline on desktop, behind one button on a phone (decision 10).
  const project = page.getByRole("combobox", { name: "Project" });
  if (info.project.name === "mobile") {
    await expect(project).toHaveCount(0);
    await page.getByTestId("artifacts-more-filters").click();
  }
  await expect(project).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Session" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Mission" })).toBeVisible();
  await project.selectOption("p-orbital");
  await expect(page).toHaveURL(/project=p-orbital/);
});

test("Library → Artifacts sits after Memory and lights Library", async ({
  page,
}) => {
  await mockBase(page);
  await mockArtifacts(page);
  await page.goto(artifactPath(SITE_ID));
  await expect(
    page.locator('.hud-topbar [data-section="library"]'),
  ).toHaveAttribute("aria-current", "true");
});

// ---------------------------------------------------------------------------------- the detail

test("a site artifact's page previews it in a sandboxed frame — never allow-same-origin", async ({
  page,
}) => {
  await mockBase(page);
  await mockArtifacts(page);
  await page.goto(ARTIFACTS_PATH);
  await page
    .locator(`[data-artifact="${SITE_ID}"]`)
    .getByRole("link", { name: "Ingest rate-limit report", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`${artifactPath(SITE_ID)}$`));
  const frame = page.getByTestId("artifact-preview");
  await expect(frame).toBeVisible();
  const sandbox = (await frame.getAttribute("sandbox")) ?? "";
  const tokens = sandbox.split(/\s+/);
  expect(tokens).not.toContain("allow-same-origin");
  expect(tokens).toEqual(
    expect.arrayContaining([
      "allow-scripts",
      "allow-forms",
      "allow-popups",
      "allow-downloads",
    ]),
  );
  expect(await frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  expect(await frame.getAttribute("src")).toMatch(
    new RegExp(`/artifacts/${SITE_ID}/$`),
  );
  await expect(
    page
      .frameLocator("[data-testid='artifact-preview']")
      .getByRole("heading", { name: "Hosted page" }),
  ).toBeVisible();
  // The real browser enforces the sandbox: the framed document is an opaque origin.
  const origin = await page
    .frames()
    .find((f) => f.url().includes(`/artifacts/${SITE_ID}/`))!
    .evaluate(() => self.origin);
  expect(origin).toBe("null");
  // A revision pin re-points the frame at `<id>@<n>`.
  await page.getByRole("button", { name: "Show r3 in the preview" }).click();
  await expect(frame).toHaveAttribute(
    "src",
    new RegExp(`/artifacts/${SITE_ID}@3/$`),
  );
});

test("share publicly needs 'I checked the files', then revoke ends the link", async ({
  page,
}) => {
  await mockBase(page);
  const st = await mockArtifacts(page);
  await page.goto(artifactPath(SITE_ID));
  const sharing = page.getByTestId("artifact-sharing");
  await expect(sharing).toContainText("Private");
  await page.getByTestId("artifact-share").click();
  const dialog = page.getByTestId("artifact-share-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Anyone with the link can open it.");
  const confirm = page.getByTestId("artifact-share-confirm");
  await expect(confirm).toBeDisabled();
  await page.getByTestId("artifact-share-checked").check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(dialog).toBeHidden();
  expect(
    st.calls.filter((c) => c.method === "POST" && c.path.endsWith("/share")),
  ).toHaveLength(1);
  await expect(sharing).toContainText("Public link live");
  await expect(page.getByTestId("artifact-public-url")).toContainText(
    `/artifacts/p/NEW`,
  );

  await page.getByTestId("artifact-revoke").click();
  await page.getByTestId("artifact-revoke-confirm").click();
  await expect(sharing).toContainText("Private");
  await expect(page.getByTestId("artifact-public-url")).toHaveCount(0);
  expect(
    st.calls.filter((c) => c.method === "DELETE" && c.path.endsWith("/share")),
  ).toHaveLength(1);
});

test("without a login, Share publicly explains why public links are off — and asks the server nothing", async ({
  page,
}) => {
  await mockBase(page, "none");
  const st = await mockArtifacts(page);
  await page.goto(artifactPath(SITE_ID));
  await expect(page.getByTestId("artifact-sharing")).toContainText(
    "Public links are off on this box.",
  );
  await page.getByTestId("artifact-share").click();
  const off = page.getByTestId("artifact-public-off");
  await expect(off).toBeVisible();
  await expect(off).toContainText("Public links are off on this box");
  await expect(off).toContainText(`/artifacts/${SITE_ID}/`.replace(/-/g, "-​"));
  expect(st.calls.some((c) => c.method === "POST")).toBe(false);
  await page.keyboard.press("Escape");
  await expect(off).toBeHidden();
});

test("the server's 409 public_off turns the share dialog into the off state", async ({
  page,
}) => {
  await mockBase(page);
  await mockArtifacts(page, { publicOff: true });
  await page.goto(artifactPath(SITE_ID));
  await page.getByTestId("artifact-share").click();
  await page.getByTestId("artifact-share-checked").check();
  await page.getByTestId("artifact-share-confirm").click();
  const off = page.getByTestId("artifact-public-off");
  await expect(off).toBeVisible();
  await expect(off).toContainText("Public links are off without a login.");
  await expect(page.getByTestId("artifact-sharing")).not.toContainText(
    "Public link live",
  );
});

// ---------------------------------------------------------------------------------- the session tab

function sessionRow(id: string, engine: string, title: string) {
  return {
    id,
    engine,
    title,
    cwd: CWD,
    project: { kind: "project", id: "p-orbital", name: "orbital-api" },
    last_mtime: NOW - 60,
    archived: false,
    favorite: false,
    running: true,
  };
}

async function mockSessionApp(page: Page, st: Partial<State> = {}) {
  await mockBase(page);
  const sessions = [
    sessionRow(SESSION_KEY, "claude", "Rate-limit the public ingest API"),
    sessionRow(NO_TOOL_KEY, "gemini", "Flaky e2e"),
  ];
  await page.route("**/api/sessions**", (r) => {
    const path = new URL(r.request().url()).pathname;
    const one = sessions.find(
      (s) =>
        path === `/api/sessions/${s.id}` ||
        path === `/api/sessions/${encodeURIComponent(s.id)}`,
    );
    if (one) return r.fulfill({ json: one });
    return r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: 2,
        facets: { projects: [], engines: ["claude", "gemini"] },
      },
    });
  });
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: null,
        root: CWD,
        entries: [],
        total: 0,
        complete: true,
        truncated: false,
      },
    }),
  );
  return mockArtifacts(page, st);
}

async function openArtifactsTab(page: Page, key: string) {
  const [engine, id] = key.split(":");
  await page.goto(`/s/${engine}/${id}`);
  await (await headAction(page, FILES_ACTION)).click();
  await expect(page.locator("[data-file-panel]")).toBeVisible();
  await page
    .locator("[data-file-panel]")
    .getByRole("tab", { name: /Artifacts/ })
    .click();
  const tab = page.getByTestId("artifacts-tab");
  await expect(tab).toBeVisible();
  return tab;
}

test("a session's ARTIFACTS tab shows its artifacts beside FILES and GIT", async ({
  page,
}) => {
  const st = await mockSessionApp(page, { sessionMode: "private" });
  const tab = await openArtifactsTab(page, SESSION_KEY);
  const tabs = page.locator("[data-file-panel]").getByRole("tab");
  await expect(tabs).toHaveText([/Files/, /Git/, /Artifacts/]);
  await expect(
    page.locator("[data-file-panel]").getByRole("tab", { name: /Artifacts/ }),
  ).toContainText("3");
  await expect(tab.locator("[data-artifact]")).toHaveCount(3);
  await expect(page.getByTestId("artifacts-tool-mode")).toContainText(
    "Private",
  );
  expect(
    st.calls.some(
      (c) =>
        c.path === "/api/artifacts" &&
        c.search.includes(`session=${encodeURIComponent(SESSION_KEY)}`),
    ),
  ).toBe(true);
  // A tile opens the artifact's page.
  await tab.locator(`[data-artifact="${SITE_ID}"]`).click();
  await expect(page).toHaveURL(new RegExp(`${artifactPath(SITE_ID)}$`));
});

test("an agent without a tool channel says so in the tab", async ({ page }) => {
  await mockSessionApp(page);
  const tab = await openArtifactsTab(page, NO_TOOL_KEY);
  await expect(tab.getByTestId("artifact-not-available")).toContainText(
    "Not available for this agent.",
  );
});

test("the activation dialog sets the mode and says what the running agent gets at restart", async ({
  page,
}) => {
  const st = await mockSessionApp(page, {
    sessionMode: null,
    inherited: "off",
  });
  await openArtifactsTab(page, SESSION_KEY);
  await page.getByRole("button", { name: "Change" }).click();
  const dialog = page.getByTestId("artifact-activation-session");
  await expect(dialog).toBeVisible();
  // Nothing set for the session: no mode is chosen, and it says where "off" comes from.
  for (const name of ["Off", "Private", "Public"])
    await expect(dialog.getByRole("radio", { name })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  await dialog.getByRole("radio", { name: "Private" }).click();
  await expect(dialog.getByRole("radio", { name: "Private" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(st.calls.find((c) => c.method === "PUT")?.body).toEqual({
    scope: "session",
    id: SESSION_KEY,
    mode: "private",
  });
  const notice = dialog.getByTestId("artifact-restart-notice");
  await expect(notice).toContainText("Takes effect when the agent restarts.");
  await expect(notice).toContainText(
    "The running claude does not have the tool yet; it gets it at restart.",
  );
  await expect(
    dialog.getByRole("button", { name: /restart now/i }),
  ).toHaveCount(0);

  // Clearing inherits again.
  await dialog.getByRole("button", { name: /^Clear/ }).click();
  await expect
    .poll(() => st.calls.filter((c) => c.method === "PUT").at(-1)?.body)
    .toEqual({ scope: "session", id: SESSION_KEY, mode: null });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("Private → Public reads 'the new mode applies when the agent restarts'", async ({
  page,
}) => {
  await mockSessionApp(page, { sessionMode: "private" });
  await openArtifactsTab(page, SESSION_KEY);
  await page.getByRole("button", { name: "Change" }).click();
  const dialog = page.getByTestId("artifact-activation-session");
  await dialog.getByRole("radio", { name: "Public" }).click();
  const notice = dialog.getByTestId("artifact-restart-notice");
  await expect(notice).toContainText(
    "The new mode applies when the claude restarts.",
  );
  await expect(notice).not.toContainText("does not have the tool yet");
});

test("the session's ⋯ menu opens the same activation dialog", async ({
  page,
}, info) => {
  test.skip(
    info.project.name === "mobile",
    "the row menu lives in the drawer; the tab path covers the phone",
  );
  await mockSessionApp(page, { sessionMode: "private" });
  await page.goto(`/s/claude/${SESSION_KEY.split(":")[1]}`);
  await page.getByRole("button", { name: "Session actions" }).first().click();
  await page
    .getByRole("menuitem", { name: "Artifacts tool for this session" })
    .click();
  const dialog = page.getByTestId("artifact-activation-session");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radio", { name: "Private" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
});

// ---------------------------------------------------------------------------------- 44 px at ≤800px

test("every artifacts control holds the 44 px floor at ≤800px", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockBase(page);
  await mockArtifacts(page);
  const short: string[] = [];
  const measure = async (scope: string) => {
    await animationsSettled(page);
    const boxes = await page.locator(scope).evaluateAll((els) =>
      els
        .filter((el) => (el as HTMLElement).offsetParent !== null)
        .map((el) => {
          const r = el.getBoundingClientRect();
          return {
            name:
              el.getAttribute("aria-label") ||
              el.textContent?.trim().slice(0, 30) ||
              el.tagName,
            h: r.height,
            w: r.width,
          };
        }),
    );
    for (const b of boxes)
      if (b.h < TOUCH_FLOOR) short.push(`${scope}: ${b.name} ${b.h}px`);
    return boxes.length;
  };
  await page.goto(ARTIFACTS_PATH);
  await expect(page.locator("[data-artifact]")).toHaveCount(3);
  expect(
    await measure(
      "[data-testid='artifacts-page'] :is(button, a[href], input, select)",
    ),
  ).toBeGreaterThan(10);
  await page.getByTestId("artifacts-more-filters").click();
  await measure("[data-testid='artifacts-filters'] select");

  await page.goto(artifactPath(SITE_ID));
  await expect(page.getByTestId("artifact-preview")).toBeVisible();
  expect(
    await measure("[data-testid='artifact-detail'] :is(button, a[href])"),
  ).toBeGreaterThan(5);
  // No sideways scroll: the long link and address bar wrap or ellipsize inside the page (§7).
  const overflow = await page
    .getByTestId("artifact-detail")
    .evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.getByTestId("artifact-share").click();
  await expect(page.getByTestId("artifact-share-dialog")).toBeVisible();
  await measure("[data-testid='artifact-share-dialog'] :is(button, label)");
  expect(short).toEqual([]);
});
