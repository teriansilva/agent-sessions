/** Library → MCP servers: a Go module from a repository link, and Update from source (#2427), on a
 *  real browser at desktop and phone size, against `mcpFixture.ts`'s stand-in for the routes (the
 *  resolver, the Go review and the update fence are pinned in `tests/test_mcp_go_source.py`). The
 *  pages, router and every request they make are real; what the requests SAID is asserted from the
 *  fake's call log.
 *
 *  Pinned: the source tile says it builds a Go module; reading a Go link words its rows for Go (the
 *  toolchain's first download shows while the module is looked up); Review shows the repository,
 *  the server's build line, then the Go block (module, version, home + mirror note, command, Go,
 *  module hash, the computed dependency count, toolchain), the third-party caution, and Install
 *  registers it by resolution_id and installs exactly the review shown; a Go refusal is amber words
 *  with the way forward and no Install; a Go server's Settings → Source is `go · module version`
 *  with its origin, no version field, and Update from source; Update from source reads the server's
 *  OWN link, shows old → new, applies it by resolution_id fenced to the record it read, installs
 *  the reviewed review_id and lands on Projects with the check reminder; an up-to-date link says
 *  so and writes nothing; a server that changed meanwhile is a 409 in words with Update again. */
import { expect, test, type Page } from "@playwright/test";

import { MCP_NEW_PATH, mcpServerPath, mcpServerUpdatePath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";
import {
  emptyFake,
  GO_HOME,
  GO_LINE,
  GO_LINK,
  GO_MODULE,
  GO_NEW_SHA,
  GO_NEW_SUM,
  GO_SHA,
  GO_SUM,
  goResolution,
  goServer,
  mockMcp,
  type Fake,
} from "./mcpFixture";

test.setTimeout(60_000);

const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const creates = (f: Fake) => f.calls.filter((c) => c.method === "POST" && c.path === "/api/mcp/servers");
const patches = (f: Fake) => f.calls.filter((c) => c.method === "PATCH");

async function noSideways(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

async function toGoReview(page: Page, f: Fake) {
  await mockMcp(page, f);
  await page.goto(MCP_NEW_PATH);
  const tile = page.getByTestId("mcp-source-kinds").locator("label").filter({ hasText: "Repository link" });
  await expect(tile).toContainText("or builds its Go module from that commit");
  await page.getByRole("radio", { name: /Repository link/ }).check();
  await page.getByRole("textbox", { name: "Repository link" }).fill(GO_LINK);
  await page.getByRole("button", { name: "Next: Review" }).click();
  await expect(page.getByTestId("mcp-step-review")).toBeVisible();
}

test("a Go link: Go progress rows, the Go facts and the build line; Install registers it by resolution_id", async ({
  page,
}) => {
  const f = emptyFake();
  f.opPolls = 4;
  f.resolveHold = "release";
  f.links[GO_LINK] = { resolution: goResolution() };
  await toGoReview(page, f);

  // Reading: the module lookup is running, and its first use downloads the toolchain.
  const rows = page.getByTestId("mcp-resolve-rows");
  await expect(rows).toHaveAttribute("data-kind", "go");
  const module = rows.locator("li[data-row=release]");
  await expect(module).toHaveAttribute("data-state", "running");
  await expect(module).toContainText("Module");
  await expect(module).toContainText("Downloading the Go toolchain (first time only)");
  await expect(rows.locator("li[data-row=repository]")).toContainText("Commit match");
  await expect(rows.locator("li[data-row=manifest]")).toContainText(`go.mod → ${GO_MODULE}`);
  await expect(page.getByTestId("mcp-op-note")).toContainText("nothing is built while the link is read");
  await expect(page.getByRole("button", { name: "Install" })).toBeDisabled();
  // The aside never claims nothing is downloaded (a Go link fetches the toolchain).
  await expect(page.getByText(/no package is downloaded yet/)).toHaveCount(0);
  f.resolveHold = undefined;

  const origin = page.getByTestId("mcp-review-origin");
  await expect(origin).toContainText(`codeberg.org/goern/forgejo-mcp @ ${GO_SHA.slice(0, 7)}`);
  await expect(origin).toContainText(GO_SHA);
  await expect(origin).toContainText(`go.mod → Go module ${GO_MODULE}`);
  await expect(origin).not.toContainText("Registry record");
  await expect(origin).toContainText("The Go module");
  await expect(page.getByTestId("mcp-origin-disclaimer")).toHaveText(GO_LINE);
  const facts = page.getByTestId("mcp-review-facts");
  await expect(facts).toHaveAttribute("data-kind", "go");
  await expect(facts).toContainText("v3.3.0 · the tag at this commit");
  await expect(facts).toContainText(GO_HOME);
  await expect(page.getByTestId("mcp-go-home-note")).toHaveText(
    "the module’s own repository · same commit as the link (the link is a mirror)",
  );
  await expect(facts).toContainText("forgejo-mcp · the module root");
  await expect(facts).toContainText("Go 1.26.0");
  await expect(facts).toContainText(`${GO_SUM} · verified against sum.golang.org`);
  await expect(page.getByTestId("mcp-go-deps")).toHaveText("46 modules in its build · each verified against sum.golang.org");
  await expect(facts).toContainText("go1.27.4 · BattleLab’s own, sha256-verified · cgo off");
  await expect(page.getByTestId("mcp-caution")).toContainText("Third-party code");
  await expect(page.getByText("Nothing has been built yet: Install builds exactly this module version and hash.")).toBeVisible();
  // The build line sits between the repository facts and the Go block.
  const [d, r] = await Promise.all([
    page.getByTestId("mcp-origin-disclaimer").boundingBox(),
    facts.boundingBox(),
  ]);
  expect(d!.y).toBeLessThan(r!.y);
  await noSideways(page);
  expect(creates(f)).toEqual([]);

  const install = page.getByRole("button", { name: "Install" });
  if (isPhone(page)) expect((await install.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await install.click();
  await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
  const [create] = creates(f);
  const body = create.body as Record<string, unknown>;
  const resolve = f.calls.find((c) => c.path === "/api/mcp/servers/resolve-link")!;
  expect(body.resolution_id).toBe((resolve.body as { request_id: string }).request_id);
  expect(body.source).toEqual({ kind: "go", package: GO_MODULE, version: "v3.3.0" });
  expect(body).not.toHaveProperty("origin");
  const installed = f.calls.find((c) => c.path.endsWith("/install"))!;
  expect((installed.body as { review_id: string }).review_id).toBe("b0c1d2e3f4a5b6c7");
});

test("a Go refusal (several commands): amber words, what was read, the way forward, no Install", async ({ page }) => {
  const f = emptyFake();
  f.links[GO_LINK] = {
    error:
      "BattleLab can't build this Go module: the module builds several commands (cmd/server, cmd/migrate), and BattleLab never picks one for you",
    details: {
      repository: "codeberg.org/goern/forgejo-mcp",
      commit: GO_SHA,
      found: [{ manifest: "go.mod", kind: "go", package: GO_MODULE, version: "" }],
    },
  };
  await toGoReview(page, f);
  const refused = page.getByTestId("mcp-link-refused");
  await expect(refused).toContainText("Can’t add this repository");
  await expect(refused).toContainText(
    "BattleLab can't build this Go module: the module builds several commands (cmd/server, cmd/migrate), and BattleLab never picks one for you.",
  );
  await expect(refused).toContainText(`go.mod → Go module ${GO_MODULE}`);
  await expect(refused).not.toContainText("(no version)");
  await expect(page.getByText(/exactly one command per server and never picks one for you/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  await noSideways(page);
  expect(creates(f)).toEqual([]);
});

test("a Go server's Settings → Source: go · module version, the origin, no version field, Update from source", async ({
  page,
}) => {
  const f = emptyFake();
  const s = goServer(f);
  await mockMcp(page, f);
  await page.goto(`${mcpServerPath(s.id)}?tab=settings`);
  const origin = page.getByTestId("mcp-source-origin");
  await expect(origin).toContainText(`go · ${GO_MODULE} v3.3.0`);
  await expect(origin).toContainText(`From a repository · codeberg.org/goern/forgejo-mcp @ ${GO_SHA.slice(0, 7)}`);
  await expect(origin).toContainText(GO_LINE);
  const panel = page.getByTestId("mcp-source-panel");
  await expect(panel.locator("input")).toHaveCount(0); // a Go version is never typed in
  const update = panel.getByTestId("mcp-update-from-source");
  await expect(update).toHaveText("Update from source");
  if (isPhone(page)) expect((await update.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // Saving the other settings sends no source.
  await page.getByTestId("mcp-settings-save").click();
  await expect(page.getByTestId("mcp-flash")).toBeVisible();
  expect(patches(f).map((c) => c.body)).toEqual([expect.not.objectContaining({ source: expect.anything() })]);
  // The ⋯ menu offers Update from source, not a version update.
  await page.getByTestId("mcp-server-menu").click();
  await expect(page.getByRole("menuitem", { name: "Update…" })).toHaveCount(0);
  await page.getByRole("menuitem", { name: "Update from source…" }).click();
  await expect(page).toHaveURL(new RegExp(`${mcpServerUpdatePath(s.id)}$`));
  await expect(page.getByTestId("mcp-update")).toBeVisible();
});

test("Update from source: the link read again, old → new, applied by resolution_id, installed, then Projects", async ({
  page,
}) => {
  const f = emptyFake();
  f.opPolls = 3;
  const s = goServer(f);
  const before = s.updated_at;
  f.links[GO_LINK] = { resolution: goResolution({ version: "v3.4.0", commit: GO_NEW_SHA, sum: GO_NEW_SUM, reviewId: "c1d2e3f4a5b6c7d8" }) };
  await mockMcp(page, f);
  await page.goto(`${mcpServerPath(s.id)}?tab=settings`);
  await page.getByTestId("mcp-update-from-source").click();

  // Reading: the same rows, the installed commit beside the head, and nothing changes meanwhile.
  const rows = page.getByTestId("mcp-resolve-rows");
  await expect(rows.locator("li[data-row=commit]")).toContainText(
    `default branch head ${GO_NEW_SHA.slice(0, 7)} · installed ${GO_SHA.slice(0, 7)}`,
  );
  await expect(page.getByTestId("mcp-op-note")).toContainText(
    `Nothing changes while the link is read: forgejo-mcp keeps v3.3.0 (${GO_SHA.slice(0, 7)})`,
  );
  const read = f.calls.find((c) => c.path === "/api/mcp/servers/resolve-link")!;
  expect(Object.keys(read.body as object).sort()).toEqual(["request_id", "server_id"]); // never a link

  const review = page.getByTestId("mcp-update-review");
  await expect(review).toBeVisible();
  await expect(page.getByRole("heading", { name: "What changes" })).toBeVisible();
  await expect(review).toContainText("the link this server was added from");
  const changes = review.getByTestId("mcp-change");
  await expect(changes).toHaveCount(3); // commit, version, module hash
  await expect(changes.nth(0)).toHaveText(`${GO_SHA.slice(0, 7)} → ${GO_NEW_SHA.slice(0, 7)}`);
  await expect(changes.nth(1)).toHaveText("v3.3.0 → v3.4.0");
  await expect(changes.nth(2)).toHaveText(`${GO_SUM.slice(0, 10)}… → ${GO_NEW_SUM.slice(0, 10)}…`);
  await expect(review).toContainText("The Go module · old → new");
  await expect(page.getByTestId("mcp-go-deps")).toHaveText("46 modules in its build · each verified against sum.golang.org");
  await expect(page.getByTestId("mcp-origin-disclaimer")).toHaveText(GO_LINE);
  await expect(review).toContainText("Install builds exactly v3.4.0 at this hash and replaces v3.3.0 in one step");
  await expect(page.getByRole("link", { name: "Cancel" })).toBeVisible();
  await noSideways(page);
  expect(patches(f)).toEqual([]);

  await page.getByRole("button", { name: "Install" }).click();
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(s.id)}\\?tab=projects$`));
  await expect(page.getByTestId("mcp-flash")).toHaveText(
    "Updated to v3.4.0 — run the checks; new sessions don't get this server until a check passes.",
  );
  const [patch] = patches(f);
  expect(patch.body).toEqual({ resolution_id: (read.body as { request_id: string }).request_id, expected_updated_at: before });
  const installed = f.calls.find((c) => c.path.endsWith("/install"))!;
  expect((installed.body as { review_id: string }).review_id).toBe("c1d2e3f4a5b6c7d8");
  expect(f.servers[0].source).toEqual({ kind: "go", package: GO_MODULE, version: "v3.4.0" });
  expect(f.servers[0].origin).toEqual({ url: GO_LINK, commit: GO_NEW_SHA });
  // The new install has no passing check yet: every project's check is to run again.
  const projectRows = page.getByTestId("mcp-project-row");
  await expect(projectRows).toHaveCount(3);
  for (const row of await projectRows.all()) await expect(row).toHaveAttribute("data-check", "not_run");
});

test("Update from source when the link is current: already up to date, nothing written", async ({ page }) => {
  const f = emptyFake();
  const s = goServer(f);
  f.links[GO_LINK] = { resolution: goResolution() };
  await mockMcp(page, f);
  await page.goto(mcpServerUpdatePath(s.id));
  await expect(page.getByRole("heading", { name: "Nothing to update" })).toBeVisible();
  await expect(page.getByTestId("mcp-update-current")).toHaveText(`Already up to date at v3.3.0 (${GO_SHA.slice(0, 7)})`);
  await expect(page.getByText(/the server’s check still counts/)).toBeVisible();
  // The aside (desktop only) says Check has nothing to do, never "needs a new check".
  if (!isPhone(page)) await expect(page.getByText(/keeps its install and its passing check/)).toBeVisible();
  await expect(page.getByText(/needs its own passing check/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  const back = page.getByRole("button", { name: "Back to Forgejo MCP" });
  if (isPhone(page)) expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await back.click();
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(s.id)}$`));
  expect(patches(f)).toEqual([]);
  expect(f.calls.filter((c) => c.path.endsWith("/install"))).toEqual([]);
});

test("Update from source when the server changed since its link was read: a 409 in words, Update again", async ({
  page,
}) => {
  const f = emptyFake();
  const s = goServer(f);
  f.links[GO_LINK] = { resolution: goResolution({ version: "v3.4.0", commit: GO_NEW_SHA, sum: GO_NEW_SUM }) };
  await mockMcp(page, f);
  await page.goto(mcpServerUpdatePath(s.id));
  await expect(page.getByTestId("mcp-update-review")).toBeVisible();
  s.updated_at += 1; // someone saved the server meanwhile
  await page.getByRole("button", { name: "Install" }).click();
  const conflict = page.getByTestId("mcp-update-conflict");
  await expect(conflict).toContainText("The server changed");
  await expect(conflict).toContainText("nothing was updated. Update again");
  await expect(conflict).toContainText("the server changed since you loaded it");
  expect(f.calls.filter((c) => c.path.endsWith("/install"))).toEqual([]);
  expect(f.servers[0].source).toEqual({ kind: "go", package: GO_MODULE, version: "v3.3.0" });
  await page.getByRole("button", { name: "Update again" }).click();
  await expect(page.getByTestId("mcp-update-review")).toBeVisible();
  expect(f.calls.filter((c) => c.path === "/api/mcp/servers/resolve-link")).toHaveLength(2);
  await page.getByRole("button", { name: "Install" }).click();
  await expect(page).toHaveURL(new RegExp(`${mcpServerPath(s.id)}\\?tab=projects$`));
});
