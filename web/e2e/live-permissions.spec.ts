import { expect, test, type Page } from "@playwright/test";
import { ID, mockLivePermissions, openPermissions } from "./livePermissionsFixture";
import { animationsSettled } from "./nightjarShell";
import { mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const setup = async (page: Page, theme: "dark" | "light", engine = "codex-api", uncertain = false) => {
  const attempts = await mockLivePermissions(page, theme, engine, uncertain);
  await page.goto(`/s/${engine}/${ID}`);
  await expect(page.getByTestId("structured-always")).toBeVisible();
  return attempts;
};

for (const theme of ["dark", "light"] as const) {
  for (const engine of ["codex-api", "claude-api", "opencode-api"]) {
    test(`enable skip in an existing ${engine} session (${theme})`, async ({ page }) => {
      const attempts = await setup(page, theme, engine);
      const dialog = await openPermissions(page);
      await expect(dialog).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
      await expect(dialog).toContainText("queued messages");
      expect(attempts).toEqual([]);
      // Measured where the dialog LANDS: it deploys in with a 6 px rise (#1531).
      await animationsSettled(page);
      for (const button of await dialog.getByRole("button").all()) {
        const box = (await button.boundingBox())!;
        expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      }
      await dialog.getByRole("button", { name: "Stop turn & enable" }).click();
      await expect(dialog).toHaveCount(0);
      await expect(page.getByTestId("structured-bypass")).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).toMatch(/^(BUTTON|TEXTAREA)$/);
      await expect(page.getByText("I will check the project guidelines.")).toBeVisible();
      expect(attempts).toHaveLength(1);
      expect(Object.keys(attempts[0])).toEqual(["operation_id"]);
      await expect(page.getByTestId("structured-request")).toHaveCount(0);
      await expect(page.getByText("waiting for your decision", { exact: true })).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  }

  test(`Approve always has equal prominence and retains explicit choices (${theme})`, async ({ page }) => {
    await setup(page, theme);
    const once = page.getByRole("button", { name: "Approve once", exact: true });
    const always = page.getByTestId("structured-always");
    await expect(always).toBeVisible();
    expect(await always.evaluate((e) => getComputedStyle(e).backgroundColor))
      .toBe(await once.evaluate((e) => getComputedStyle(e).backgroundColor));
    expect((await always.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    const ratio = await always.evaluate((el) => {
      const style = getComputedStyle(el);
      const luminance = (css: string) => {
        const rgb = css.match(/[\d.]+/g)!.slice(0, 3).map(Number).map((v) => {
          v /= 255;
          return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const fg = luminance(style.color), bg = luminance(style.backgroundColor);
      return (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05);
    });
    expect(ratio).toBeGreaterThanOrEqual(4.5);
    // The fixture proposes one grant: no list to open, its scope is on hover.
    await expect(always).toHaveAttribute("title", /for the rest of this session/);
    await expect(always).not.toHaveAttribute("aria-expanded");
  });

  test(`existing-session permissions are reachable in the map's single chrome (${theme})`, async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "map windows are desktop-only");
    const attempts = await setup(page, theme);
    const row = { id: `codex-api:${ID}`, engine: "codex-api", uuid: ID, short_uuid: ID.slice(0, 8),
      cwd: "/home/u/proj", project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
      last_mtime: Math.floor(Date.now() / 1000), first_user_message: "Review project", title: "Review project",
      sticky: false, archived: false, ai_summary: "" };
    await page.route("**/api/sessions?**", (r) => r.fulfill({ json: {
      sessions: [row], next_offset: null, total: 1, facets: { projects: [], engines: [] },
    } }));
    await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: row }));
    await page.route("**/api/config", (r) => r.fulfill({ json: {
      csrf: "x", new_session_engines: ["codex-api"], terminal_backend: "ws", auth_mode: "none",
      default_project: "/home/u/proj", overview_expanded: ["project:p1"], projects_hidden: [], project_names: {}, theme,
    } }));
    await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: {
      projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }],
    } }));
    await page.goto("/overview");
    await page.locator(".tr-ov-chip").first().click();
    const win = page.locator(`[data-session-window="codex-api:${ID}"]`);
    await expect(win.getByTestId("structured-always")).toBeVisible();
    await expect(win.locator("[data-panel-head]")).toHaveCount(0);
    await expect(win.locator("[data-window-menu]")).toHaveCount(1);
    const action = win.locator("[data-head-action='skip-permissions']");
    if (await action.isVisible()) await action.click();
    else {
      await win.locator("[data-window-menu]").click();
      await page.getByRole("menuitem", { name: "Enable Skip permissions for this session" }).click();
    }
    await page.getByRole("dialog").getByRole("button", { name: "Stop turn & enable" }).click();
    await expect(win.getByTestId("structured-bypass-banner")).toBeVisible();
    expect(attempts).toHaveLength(1);
  });
}

test("uncertain containment survives reload and retries the same operation", async ({ page }) => {
  const attempts = await setup(page, "dark", "codex-api", true);
  const dialog = await openPermissions(page);
  await dialog.getByRole("button", { name: "Stop turn & enable" }).click();
  await expect(dialog.getByRole("alert")).toContainText("not confirmed");
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  const approval = page.getByTestId("structured-request");
  for (const button of await approval.getByRole("button").all()) await expect(button).toBeDisabled();
  await page.reload();
  await expect(page.getByTestId("structured-bypass")).toHaveCount(0);
  for (const button of await page.getByTestId("structured-request").getByRole("button").all()) {
    await expect(button).toBeDisabled();
  }
  await page.getByRole("button", { name: "Retry enabling", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Retry enabling" }).click();
  await expect(page.getByTestId("structured-bypass")).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[1]).toEqual(attempts[0]);
});

test("cancel and Escape restore focus without changing permission mode", async ({ page }) => {
  const attempts = await setup(page, "dark");
  const dialog = await openPermissions(page);
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(attempts).toEqual([]);
  await expect(page.getByTestId("structured-bypass")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).toBe("BUTTON");
});

test("a late pre-enable snapshot never brings back approval buttons", async ({ page }) => {
  await page.clock.install();
  await setup(page, "dark");
  const path = `/api/structured/sessions/codex-api:${ID}`;
  const before = await page.evaluate(async (url) => (await fetch(url)).json(), path);
  const dialog = await openPermissions(page);
  await dialog.getByRole("button", { name: "Stop turn & enable" }).click();
  await expect(page.getByTestId("structured-bypass")).toBeVisible();
  let oldReads = 0;
  await page.route(new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}$`), async (r) => {
    oldReads++;
    await r.fulfill({ json: before });
  });
  await page.clock.fastForward(11_000);
  await expect.poll(() => oldReads).toBeGreaterThan(0);
  await expect(page.getByTestId("structured-request")).toHaveCount(0);
  await expect(page.getByTestId("structured-bypass")).toBeVisible();
});

test("unfinished permission changes disable new Send now selections", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "codex-api", "steer");
  Object.assign(fixture.state, { can_enable_bypass: true, bypass: false,
    permission_change: { state: "stopping", operation_id: "33333333-2222-4333-8444-555555555555" } });
  await page.goto(`/s/codex-api/${ID}`);
  await expect(page.getByRole("button", { name: "Send now", exact: true })).toBeDisabled();
  expect(fixture.calls).toEqual([]);
});

test("permission containment retains recovery of an earlier Send now receipt", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "codex-api", "steer");
  await page.goto(`/s/codex-api/${ID}`);
  fixture.fail(true, 503);
  await page.getByRole("button", { name: "Send now", exact: true }).click();
  const retry = page.getByRole("button", { name: "Retry Send now", exact: true });
  await expect(retry).toBeEnabled();
  Object.assign(fixture.state, { can_enable_bypass: true, bypass: false,
    permission_change: { state: "stopping", operation_id: "33333333-2222-4333-8444-555555555555" } });
  fixture.state.revision++;
  await expect(page.getByRole("button", { name: "Retry enabling", exact: true })).toBeVisible();
  await page.route("**/send-now", async (r) => {
    fixture.calls.push(r.request().postDataJSON());
    await r.fulfill({ json: { handoff: "sent", operation_id: fixture.calls[0].operation_id } });
  });
  await retry.click();
  await expect(retry).toBeHidden();
  expect(fixture.calls).toHaveLength(2);
  expect(fixture.calls[1]).toEqual(fixture.calls[0]);
});
