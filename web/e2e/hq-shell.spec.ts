/** HQ's own shell (#2404, epic #2070 B3), in a real browser on desktop and mobile: Settings →
 *  AI → HQ → Authority has the RISKY "Runs commands" row with its own "also after untrusted text"
 *  switch, and says when this host can't contain commands; a command card shows the command, its
 *  folder and timeout verbatim, Run sends exactly `{decision: "confirm"}`, a running command
 *  offers Stop (`{decision: "stop"}`), a command HQ ran itself reads "Ran by HQ" with its exit
 *  code and output and no buttons, and a file write that went stale says nothing was written.
 *  Every control is a 44 px target on a phone and nothing scrolls sideways. Screenshots in both
 *  themes go to the test's output dir (the PR attaches them). */
import { expect, test, type Page } from "@playwright/test";

import { mockShell } from "./callFixture";
import { PID, noSidewaysScroll, shell } from "./askOperationShell";
import { openAsk, pickConversation } from "./askSidebar";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const ALL = ["reversible", "starts", "destructive", "external", "widens", "runs_commands"];

async function authority(page: Page, shellState: { available: boolean; reason: string }) {
  const saves: Record<string, unknown>[] = [];
  let untrusted: Record<string, boolean> = { starts: false, destructive: false, runs_commands: false };
  let revision = 7;
  const classes = Object.fromEntries(ALL.map((c) => [c, "act"]));
  const view = () => ({
    classes,
    preset: "full",
    revision,
    actable: ["reversible", "starts", "destructive", "runs_commands"],
    risky: ["destructive", "external", "widens", "runs_commands"],
    untrusted,
    untrusted_classes: ["starts", "destructive", "runs_commands"],
    shell_host: shellState,
  });
  await mockShell(page);
  await page.route("**/api/hq/authority", async (r) => {
    if (r.request().method() === "GET") return r.fulfill({ json: view() });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    saves.push(body);
    untrusted = { ...untrusted, ...(body.untrusted as Record<string, boolean>) };
    revision += 1;
    return r.fulfill({ json: view() });
  });
  await page.route("**/api/hq/acts", (r) => r.fulfill({ json: { acts: [] } }));
  return saves;
}

async function openThread(page: Page) {
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const card = panel.getByTestId("ask-command");
  await expect(card).toBeVisible();
  return card;
}

const RUN = {
  action_id: PID,
  kind: "command",
  action: "run",
  command: "git status --short && npm test",
  cwd: "/home/u/src/web-shop",
  timeout: 120,
  text: "git status --short && npm test",
};

for (const theme of ["dark", "light"] as const) {
  test(`authority: the Runs commands row is RISKY and has its own untrusted switch · ${theme}`, async ({ page }, info) => {
    const saves = await authority(page, { available: true, reason: "" });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const row = page.getByTestId("hq-authority-row-runs_commands");
    await expect(row).toContainText("Runs commands");
    await expect(row.getByText("RISKY", { exact: true }).first()).toBeVisible();
    await expect(row.getByTestId("hq-authority-shell-unavailable")).toHaveCount(0);
    const sw = row.getByTestId("hq-authority-untrusted-runs_commands");
    await expect(sw).toContainText("command output");
    await expect(row.getByRole("radio", { name: "Act" })).toHaveAttribute("aria-checked", "true");
    if (PHONE(page)) {
      for (const b of await row.getByRole("radio").all())
        expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect((await sw.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    await noSidewaysScroll(page);
    await row.screenshot({ path: info.outputPath(`hq-b3-authority-row-${theme}.png`) });
    await sw.getByRole("switch").click();
    await expect.poll(() => saves.length).toBe(1);
    expect(saves[0]).toEqual({ revision: 7, untrusted: { runs_commands: true } });
    await expect(sw).toContainText("run commands as you");
  });

  test(`authority: a host that can't contain commands says so on the row · ${theme}`, async ({ page }, info) => {
    await authority(page, {
      available: false,
      reason: "no systemd user manager is reachable, so commands can't be contained here",
    });
    await page.goto("/settings/ai-hq");
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    const note = page.getByTestId("hq-authority-shell-unavailable");
    await expect(note).toContainText("Unavailable on this host");
    await expect(note).toContainText("HQ does not run commands here");
    await page
      .getByTestId("hq-authority-row-runs_commands")
      .screenshot({ path: info.outputPath(`hq-b3-authority-unavailable-${theme}.png`) });
  });

  test(`a command card shows the command verbatim; Run, then Stop, send only the decision · ${theme}`, async ({ page }, info) => {
    const fake = await shell(page, {
      ...RUN,
      state: "pending",
      reason: "",
      can_run: true,
      can_dismiss: true,
      can_stop: false,
    });
    const bodies: Record<string, unknown>[] = [];
    await page.route(`**/api/ask/proposals/${PID}/decide`, (r) => {
      const body = r.request().postDataJSON() as Record<string, unknown>;
      bodies.push(body);
      const p = fake.conversations[0].turns[0].proposals![0];
      const next =
        body.decision === "stop"
          ? { state: "stopped", reason: "stopped by the Commander", can_stop: false }
          : { state: "deciding", can_run: false, can_dismiss: false, can_stop: true };
      Object.assign(p, next);
      return r.fulfill({ status: body.decision === "stop" ? 200 : 202, json: { proposal: next } });
    });
    const c = await openThread(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-command-text")).toHaveText(RUN.command);
    await expect(c.getByTestId("ask-command-cwd")).toHaveText(RUN.cwd);
    await expect(c).toContainText("120 s");
    const run = c.getByTestId("ask-command-run");
    if (PHONE(page)) {
      expect((await run.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      const dismiss = c.getByTestId("ask-command-dismiss");
      expect((await dismiss.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    await noSidewaysScroll(page);
    await c.screenshot({ path: info.outputPath(`hq-b3-command-pending-${theme}.png`) });
    await run.click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({ decision: "confirm" });
    const stop = c.getByTestId("ask-command-stop");
    await expect(stop).toBeVisible();
    if (PHONE(page)) expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await c.screenshot({ path: info.outputPath(`hq-b3-command-running-${theme}.png`) });
    await stop.click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[1]).toEqual({ decision: "stop" });
    await expect(c.getByTestId("ask-command-outcome")).toContainText("Stopped");
  });

  test(`a command HQ ran reads Ran by HQ with its exit code and output, no buttons · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      ...RUN,
      state: "failed",
      reason: "it exited with code 1",
      exit_code: 1,
      ms: 8400,
      output:
        " M src/cart.ts\nFAIL src/cart.test.ts\n  ● total › applies the discount once\n    Expected: 90\n    Received: 81\nTests: 1 failed, 41 passed, 42 total",
      truncated: false,
      can_run: false,
      can_dismiss: false,
      can_stop: false,
      decided_by: "hq",
    });
    const c = await openThread(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-command-kicker")).toHaveText("Ran by HQ");
    await expect(c).toContainText("Exit 1");
    await expect(c.getByTestId("ask-command-output")).toContainText("Tests: 1 failed");
    await expect(c.getByTestId("ask-command-outcome")).toContainText("Failed");
    await expect(c.getByRole("button")).toHaveCount(0);
    await expect(c.getByTestId("ask-command-by-hq")).toBeVisible();
    await noSidewaysScroll(page);
    await c.screenshot({ path: info.outputPath(`hq-b3-command-ran-${theme}.png`) });
  });

  test(`a file write that went stale says nothing was written · ${theme}`, async ({ page }, info) => {
    await shell(page, {
      action_id: PID,
      kind: "command",
      action: "write",
      path: "/home/u/src/web-shop/src/cart.ts",
      cwd: "/home/u/src/web-shop/src",
      timeout: 0,
      new: false,
      size: 2150,
      lines: 88,
      preview: "export function total(items: Item[]) {\n  return items.reduce(add, 0);\n}\n",
      preview_cut: true,
      workspace: false,
      text: "Replace /home/u/src/web-shop/src/cart.ts",
      state: "stale",
      reason: "the file changed since HQ read it; nothing was written",
      can_run: false,
      can_dismiss: false,
      can_stop: false,
    });
    const c = await openThread(page);
    await page.evaluate((value) => (document.documentElement.dataset.theme = value), theme);
    await expect(c.getByTestId("ask-command-path")).toHaveText("/home/u/src/web-shop/src/cart.ts");
    await expect(c.getByTestId("ask-command-outcome")).toContainText("Out of date");
    await expect(c.getByTestId("ask-command-outcome")).toContainText("nothing was written");
    await expect(c.getByRole("button")).toHaveCount(0);
    await c.screenshot({ path: info.outputPath(`hq-b3-write-stale-${theme}.png`) });
  });
}
