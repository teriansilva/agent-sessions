/** Library → MCP servers against the REAL app (#2163 P1c): its registry, its Variables store and its
 *  check, in a throwaway home. No MCP server is reachable from here (a remote one needs https with a
 *  verified certificate, and the registry admits a plain-http loopback fixture only through a
 *  test-only module flag the running app never sets), so the check here FAILS, and that is the
 *  point: the page shows the real failure with its step, and nothing pretends it passed. The
 *  passing path, Enable and the server page's tabs are covered against a stand-in in
 *  `mcp-servers.spec.ts`. */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { MCP_NEW_PATH, MCP_PATH, mcpServerUpdatePath } from "../src/lib/routes";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";

test.describe.configure({ mode: "serial" });
test.setTimeout(120_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

let stack: Stack;
const base = () => `http://127.0.0.1:${stack.appPort}`;
const SECRET = "test-only-mcp-credential-value";

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
  const prefs = join(stack.home, ".config", "agent-sessions");
  mkdirSync(prefs, { recursive: true });
  writeFileSync(join(prefs, "prefs.json"), JSON.stringify({ onboarded: true, whats_new_seen: "0.20.0" }));
});
test.afterAll(async () => {
  await stack?.stop();
});

async function read(page: Page, path: string) {
  const r = await page.request.get(`${base()}${path}`);
  expect(r.status(), path).toBe(200);
  return { json: await r.json(), text: JSON.stringify(await r.json()) };
}

test("an empty registry is the empty state, not an error", async ({ page }) => {
  await page.goto(`${base()}${MCP_PATH}`);
  await expect(page.getByTestId("mcp-empty")).toContainText("No MCP servers yet");
  await expect(page.getByTestId("mcp-read-error")).toHaveCount(0);
});

test("register a remote server with a new credential; the real check's failure is shown, the value never", async ({
  page,
}) => {
  await page.goto(`${base()}${MCP_NEW_PATH}`);
  await page.getByLabel("Server URL").fill("https://mcp.example.invalid/mcp");
  await expect(page.getByLabel("Name", { exact: true })).toHaveValue("example");
  await page.getByRole("button", { name: "Next: Review" }).click();
  await expect(page.getByTestId("mcp-review-facts")).toContainText("mcp.example.invalid");
  await page.getByRole("button", { name: "Next: Settings" }).click();

  await page.getByTestId("mcp-add-setting").click();
  await page.getByLabel("Setting 1 name").fill("Authorization");
  await page.getByRole("radio", { name: "Credential" }).click();
  await page.getByLabel("New credential value for Authorization").fill(SECRET);
  await page.getByLabel("Prefix for Authorization").fill("Bearer ");
  await page.getByRole("button", { name: "Save & check" }).click();

  // The real check cannot reach `.invalid`: it fails, at a named step, and offers the way back.
  const report = page.getByTestId("mcp-check-report");
  await expect(report).toHaveAttribute("data-outcome", "failed", { timeout: 60_000 });
  await expect(report).toContainText("Check failed");
  await expect(report.getByRole("button", { name: "Edit credentials" })).toBeVisible();

  // The real stores: the credential is a Variables secret with no value in any answer, and the
  // server names it.
  const vars = await read(page, "/api/variables");
  const v = vars.json.variables.find((x: { name: string }) => x.name === "example_authorization");
  expect(v).toMatchObject({ kind: "secret", set: true });
  expect(v).not.toHaveProperty("value");
  const servers = await read(page, "/api/mcp/servers");
  expect(servers.json.servers).toHaveLength(1);
  expect(servers.json.servers[0].settings).toEqual([
    { key: "Authorization", kind: "credential", variable: "example_authorization", prefix: "Bearer ", deliver: "header" },
  ]);
  const detail = await read(page, `/api/mcp/servers/${servers.json.servers[0].id}`);
  expect(detail.json.checks[0]).toMatchObject({ scope: "shared", outcome: "failed" });
  for (const answer of [vars.text, servers.text, detail.text]) expect(answer).not.toContain(SECRET);
  expect(await page.locator("body").evaluate((b) => b.innerHTML)).not.toContain(SECRET);
});

test("Update from source is only for a repository-link server; a Go module is never typed in (#2427)", async ({ page }) => {
  const servers = await read(page, "/api/mcp/servers");
  const sid = servers.json.servers[0].id as string;
  // The page offers no update from a source this server doesn't have, and reads nothing.
  await page.goto(`${base()}${mcpServerUpdatePath(sid)}`);
  await expect(page.getByTestId("mcp-update-no-origin")).toContainText("wasn’t added from a repository link");
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  await page.goto(`${base()}${MCP_PATH}/${sid}`);
  await page.getByTestId("mcp-server-menu").click();
  await expect(page.getByRole("menuitem", { name: "Update from source…" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // The real routes say the same in words: no link to read again, and no typed-in Go module.
  const answers = await page.evaluate(async (sid) => {
    const csrf = (await (await fetch("/api/config")).json()).csrf as string;
    const headers = { "X-CSRF-Token": csrf, "Content-Type": "application/json" };
    const post = async (path: string, body: unknown) => {
      const r = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
      return { status: r.status, detail: ((await r.json()) as { detail?: string }).detail ?? "" };
    };
    return {
      update: await post("/api/mcp/servers/resolve-link", { server_id: sid, request_id: crypto.randomUUID() }),
      go: await post("/api/mcp/servers", {
        name: "typed-go",
        source: { kind: "go", package: "git.example.org/x/server", version: "v1.0.0" },
      }),
    };
  }, sid);
  expect(answers.update).toEqual({
    status: 422,
    detail: "this server was not added from a repository link; update it by version",
  });
  expect(answers.go.status).toBe(422);
  expect(answers.go.detail).toMatch(/a Go module is added only from its repository link/);
  expect((await read(page, "/api/mcp/servers")).json.servers).toHaveLength(1);
});

test("the server page shows the failed check in Activity, and Remove keeps the credential", async ({ page }) => {
  await page.goto(`${base()}${MCP_PATH}`);
  const row = page.getByTestId("mcp-row");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("remote · mcp.example.invalid");
  await row.click();
  await expect(page.getByTestId("mcp-server-page")).toHaveAttribute("data-server", "example");
  await page.getByRole("tab", { name: "Activity" }).click();
  await expect(page.getByTestId("mcp-activity").locator(":scope > li").first()).toHaveAttribute("data-outcome", "failed");

  await page.getByTestId("mcp-server-menu").click();
  await page.getByRole("menuitem", { name: "Remove…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${MCP_PATH}$`));
  await expect(page.getByTestId("mcp-empty")).toBeVisible();
  const vars = await read(page, "/api/variables");
  expect(vars.json.variables.map((x: { name: string }) => x.name)).toContain("example_authorization");
});
