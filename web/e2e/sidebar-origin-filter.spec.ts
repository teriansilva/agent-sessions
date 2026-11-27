import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// #2437: sessions an automation LAUNCHED are hidden from the sidebar list by default. The list
// asks `/api/sessions?origin=operator`, says what that hides ("N started by automations, hidden"),
// and Started by → Auto / All (or the line's Show) brings them back. Runs on both projects: on a
// phone the sidebar is the off-canvas drawer and every control keeps the 44px touch floor.

const row = (id: string, title: string, origin: "operator" | "automation") => ({
  id: `claude:${id}`,
  engine: "claude",
  uuid: id,
  short_uuid: id.slice(0, 8),
  cwd: "/x",
  project: { kind: "folder", id: "/x", name: "/x" },
  last_mtime: 1,
  first_user_message: title,
  title,
  sticky: false,
  archived: false,
  origin,
});
const ALL = [
  row("aaaaaaaa-0000-0000-0000-000000000001", "Refactor the invoice exporter", "operator"),
  row("aaaaaaaa-0000-0000-0000-000000000002", "Write the Q4 migration plan", "operator"),
  row("aaaaaaaa-0000-0000-0000-000000000003", "Review PR 812 for Sophia", "automation"),
];
const AUTO_TITLE = "Review PR 812 for Sophia";

async function setup(
  page: Page,
  project: string,
  opts: { unavailable?: boolean } = {},
): Promise<(string | null)[]> {
  const seen: (string | null)[] = [];
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route("**/api/sessions?**", (r) => {
    const origin = new URL(r.request().url()).searchParams.get("origin");
    seen.push(origin);
    if (opts.unavailable) {
      // The automation store could not be read: the list stays whole and says so.
      return r.fulfill({
        json: {
          sessions: ALL.map((s) => ({ ...s, origin: null })),
          next_offset: null,
          total: ALL.length,
          facets: { projects: [], engines: ["claude"] },
          ...(origin ? { origin_filter_unavailable: true } : {}),
        },
      });
    }
    const sessions = origin ? ALL.filter((s) => s.origin === origin) : ALL;
    const hidden = ALL.filter((s) => s.origin === "automation");
    return r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: { projects: [], engines: ["claude"], origins: { operator: 2, automation: 1 } },
        ...(origin === "operator" ? { origin_hidden: { count: hidden.length, needs_you: 1 } } : {}),
      },
    });
  });
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));

  await page.goto(NEW_SESSION_PATH);
  if (project === "mobile") {
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
  return seen;
}

test("automation-launched sessions are hidden by default, said, and one press away (#2437)", async ({
  page,
}, testInfo) => {
  const seen = await setup(page, testInfo.project.name);
  const sidebar = page.locator("aside.sidebar");

  // Default: the list asks for sessions you started; the automation's session is not listed.
  await expect.poll(() => seen.at(-1)).toBe("operator");
  await expect(sidebar.getByText("Refactor the invoice exporter")).toBeVisible();
  await expect(sidebar.getByText(AUTO_TITLE)).toHaveCount(0);
  // ... and the list says what it hides, including what needs you.
  const line = sidebar.getByTestId("origin-hidden");
  await expect(line).toBeVisible();
  await expect(line).toContainText("1 started by automations, hidden");
  await expect(line).toContainText("1 need you");

  const group = sidebar.getByRole("radiogroup", { name: "Started by" });
  await expect(group).toBeVisible();
  await expect(group.getByRole("radio", { name: /You/ })).toHaveAttribute("aria-checked", "true");

  if (testInfo.project.name === "mobile") {
    for (const el of [
      group.getByRole("radio", { name: /You/ }),
      group.getByRole("radio", { name: /Auto/ }),
      group.getByRole("radio", { name: /All/ }),
      line.getByRole("button", { name: /Show/ }),
    ]) {
      const box = (await el.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }

  // Show = Started by → All: every session, no origin sent, the line gone.
  await line.getByRole("button", { name: /Show/ }).click();
  await expect.poll(() => seen.at(-1)).toBeNull();
  await expect(sidebar.getByText(AUTO_TITLE)).toBeVisible();
  await expect(sidebar.getByText("Refactor the invoice exporter")).toBeVisible();
  await expect(group.getByRole("radio", { name: /All/ })).toHaveAttribute("aria-checked", "true");
  await expect(sidebar.getByTestId("origin-hidden")).toHaveCount(0);
  await expect(page.getByTestId("filters-toggle")).toContainText("1 filter");

  // Auto: only what automations launched.
  await group.getByRole("radio", { name: /Auto/ }).click();
  await expect.poll(() => seen.at(-1)).toBe("automation");
  await expect(sidebar.getByText(AUTO_TITLE)).toBeVisible();
  await expect(sidebar.getByText("Refactor the invoice exporter")).toHaveCount(0);

  // The keyboard: arrows move AND pick, focus follows (a radio group's one tab stop).
  const auto = group.getByRole("radio", { name: /Auto/ });
  await auto.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(group.getByRole("radio", { name: /You/ })).toBeFocused();
  await expect(group.getByRole("radio", { name: /You/ })).toHaveAttribute("aria-checked", "true");
  await expect.poll(() => seen.at(-1)).toBe("operator");
  await expect(sidebar.getByText(AUTO_TITLE)).toHaveCount(0);
});

test("an unreadable automation store is said, and the list stays whole (#2437)", async ({
  page,
}, testInfo) => {
  const seen = await setup(page, testInfo.project.name, { unavailable: true });
  const sidebar = page.locator("aside.sidebar");
  await expect.poll(() => seen.at(-1)).toBe("operator");
  await expect(sidebar.getByTestId("origin-unavailable")).toContainText(
    "Couldn’t tell which sessions automations started — showing all.",
  );
  await expect(sidebar.getByText(AUTO_TITLE)).toBeVisible();
  // Unknown counts, default choice: no control claiming numbers it does not have.
  await expect(sidebar.getByRole("radiogroup", { name: "Started by" })).toHaveCount(0);
  await expect(sidebar.getByTestId("origin-hidden")).toHaveCount(0);
});
