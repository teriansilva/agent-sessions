/** HQ's create card for an MCP server from a Go repository link (#2427), in a real browser, desktop
 *  and mobile. What a DOM emulator cannot prove: that every label sits in its own column and never
 *  runs into its value ("DEPENDENCIES57" on the #2353 card at phone width), that the module path and
 *  hash wrap inside the card with nothing scrolling sideways, and that the build line is the last
 *  thing above Register. The card's content itself is pinned in tests/test_ask_mcp.py. */
import { expect, test } from "@playwright/test";

import { minTarget, noSidewaysScroll, open, PID, shell } from "./askOperationShell";

const MODULE = "git.b4mad.industries/agentic-forges/forgejo-mcp/v3";
const GO_LINE =
  "Built from this commit with the Go toolchain. No install scripts run; every download is verified against sum.golang.org.";

const CARD = {
  action_id: PID,
  kind: "operation",
  op: "mcp.create",
  risk: "reversible",
  label: "Add MCP server",
  verb: "Register",
  title: "forgejo-mcp",
  text: `Registers the MCP server “forgejo-mcp”: the Go module ${MODULE} v3.3.0 that codeberg.org/goern/forgejo-mcp declares at 9c3fa38. It builds, enables and runs nothing yet: next come install (a build), credentials, a check, then enabling it for projects.`,
  needs: [],
  state: "pending",
  summary: "",
  reason: "",
  can_confirm: true,
  can_dismiss: true,
  content: true,
};

const CONTENT = {
  kind: "mcp_link",
  link: {
    repository: "codeberg.org/goern/forgejo-mcp",
    commit: "9c3fa38",
    manifest: "go.mod",
    kind: "go",
    package: MODULE,
    version: "v3.3.0",
    registry: "proxy.golang.org",
    record_field: "",
    names: "",
    integrity: "h1:a7TU1kvcAEAt",
    dependencies: 46,
    install_scripts: "",
    runs: "forgejo-mcp (stdio), built with go1.27.4",
    license: "",
    disclaimer: GO_LINE,
    home: "git.b4mad.industries/agentic-forges/forgejo-mcp",
    mirror: true,
    ref: "refs/tags/v3.3.0",
    go: "1.26.0",
    command: MODULE,
    entrypoint: "forgejo-mcp",
    toolchain: "1.27.4",
  },
  server: JSON.stringify({ name: "forgejo-mcp", source: { kind: "go", package: MODULE, version: "v3.3.0" } }, null, 2),
};

test("a Go link's card: labels beside their values, the Go facts, the build line above Register", async ({ page }) => {
  await shell(page, CARD);
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) => r.fulfill({ json: CONTENT }));
  const c = await open(page);
  const body = c.getByTestId("ask-mcp-link");
  await expect(body).toHaveAttribute("data-kind", "go");
  await expect(body).toContainText("same commit as the link (the link is a mirror)");
  await expect(body).toContainText("46 modules in its build · each verified against sum.golang.org");
  await expect(body).toContainText("go1.27.4 · BattleLab’s own, sha256-verified · cgo off");

  // Every label ends before its value starts: nothing runs together.
  const pairs = await body.locator("dt").evaluateAll((dts) =>
    dts.map((dt) => {
      const dd = dt.nextElementSibling as HTMLElement;
      const a = dt.getBoundingClientRect();
      const b = dd.getBoundingClientRect();
      // The text's own extent, not the grid cell's: an overflowing label paints past its cell.
      const range = document.createRange();
      range.selectNodeContents(dt);
      const text = range.getBoundingClientRect();
      return { label: dt.textContent, textRight: text.right, cellRight: a.right, valueLeft: b.left };
    }),
  );
  expect(pairs.length).toBeGreaterThan(8);
  for (const p of pairs) {
    expect(p.textRight, `${p.label} ends before its value`).toBeLessThanOrEqual(p.valueLeft);
    expect(p.textRight, `${p.label} stays in its cell`).toBeLessThanOrEqual(p.cellRight + 0.5);
  }

  const line = c.getByTestId("ask-mcp-disclaimer");
  await expect(line).toHaveText(GO_LINE);
  const register = c.getByTestId("ask-operation-confirm");
  await expect(register).toHaveText("Register");
  await expect(register).toBeEnabled();
  const [l, r] = [(await line.boundingBox())!, (await register.boundingBox())!];
  expect(l.y + l.height).toBeLessThanOrEqual(r.y);
  if ((page.viewportSize()?.width ?? 1280) <= 800) await minTarget(register);
  await noSidewaysScroll(page);
});
