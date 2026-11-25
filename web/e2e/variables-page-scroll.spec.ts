import { expect, test, type Page } from "@playwright/test";

// Library → Variables scrolls (operator report, 2026-11-18). The routed pane (`.terminal-pane`)
// is `overflow: hidden`, so a routed page owns its own scroll. The Variables page did not: a
// secret editor taller than the viewport was clipped, and its project list was reachable only
// by Tab. Playwright's `click()` scrolls an element into view programmatically, which works even
// in a clipped box — so this test scrolls the way a person does (a wheel on desktop, a touch
// swipe on a phone) and never clicks or focuses its way down.

const LIMITS = { variables_max: 100, value_max: 2000, name_max: 32, secret_min: 8, collections_max: 50 };
const PROJECTS = Array.from({ length: 14 }, (_, i) => ({
  id: `p${i}`,
  name: `Project ${String(i + 1).padStart(2, "0")}`,
  color: "#e8a33c",
  folders: [],
  default_folder: "",
  archived: false,
}));

async function boot(page: Page) {
  const rows = [
    {
      id: "id-key", name: "debug_ssh_key", kind: "secret_file",
      definition: { value_type: "text", label: "Debug SSH key", help: "ed25519 key for the build hosts." },
      set: true, needs_reentry: false, agent_access: true, projects: [],
      file: { name: "id_ed25519", size: 419 }, created_at: 1, updated_at: 10,
    },
  ];
  await page.route("**/api/config", (r) => r.fulfill({ json: { csrf: "x", new_session_engines: [], terminal_backend: "ws", auth_mode: "none" } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: PROJECTS } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/notifications**", (r) => r.fulfill({ json: { notifications: [] } }));
  await page.route(/\/api\/variables(\/.*)?$/, (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path === "/api/variables") {
      return r.fulfill({ json: { variables: rows, collections: [], migration_required: false, limits: LIMITS } });
    }
    if (path.endsWith("/access-log")) return r.fulfill({ json: { entries: [] } });
    return r.fulfill({ status: 404, json: { detail: "unknown" } });
  });
}

async function scrollDown(page: Page, isMobile: boolean) {
  const vp = page.viewportSize()!;
  const x = vp.width / 2;
  const y = vp.height / 2;
  if (isMobile) {
    // A finger, through Chromium's own touch input (as mission-objectives-dnd.spec.ts swipes):
    // `touch-action` and the scroll containers decide what pans — never a scrollTop write.
    const cdp = await page.context().newCDPSession(page);
    const from = vp.height * 0.8;
    const to = vp.height * 0.2;
    for (let swipe = 0; swipe < 8; swipe++) {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y: from }] });
      for (let k = 1; k <= 10; k++) {
        await cdp.send("Input.dispatchTouchEvent", {
          type: "touchMove",
          touchPoints: [{ x, y: from + ((to - from) * k) / 10 }],
        });
      }
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    }
    await cdp.detach();
  } else {
    await page.mouse.move(x, y);
    for (let i = 0; i < 12; i++) await page.mouse.wheel(0, 600);
  }
}

test("the secret editor scrolls to its last project (wheel / touch, never focus)", async ({ page, isMobile }) => {
  await page.setViewportSize(isMobile ? { width: 412, height: 700 } : { width: 1280, height: 720 });
  await boot(page);
  await page.goto("/library/variables");
  await page.getByRole("article", { name: "debug_ssh_key" }).getByRole("button", { name: "Edit debug_ssh_key" }).click();
  const last = page.getByRole("checkbox", { name: "Project 14" });
  await expect(last).toBeAttached();
  await expect(last).not.toBeInViewport(); // the precondition: the editor is taller than the screen

  await scrollDown(page, isMobile);

  await expect(last).toBeInViewport();
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeInViewport();
});
