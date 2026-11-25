/** Library → MCP servers → Add from a repository link (#2353), on a real browser at desktop and phone
 *  size, against `mcpFixture.ts`'s stand-in for the routes (the resolver itself is pinned in
 *  `tests/test_mcp_gitsource.py`). The pages, router and every request they make are real; what
 *  the requests SAID is asserted from the fake's call log.
 *
 *  Pinned: the fourth source kind replaces the old "Git repository · Later" tile and says it is
 *  selected in words; Review reads the link (progress, Stop reading, Cancel — nothing registered);
 *  a resolved link shows where the package comes from, "Installs the published release; not built
 *  from this commit." and P1b's release facts, and Install registers it BY resolution_id and
 *  installs exactly the lock shown; a refusal (both manifests) and an unpublished repository are
 *  amber words with no Install at all; the server page's Settings shows the display-only origin. */
import { expect, test, type Page } from "@playwright/test";

import { MCP_NEW_PATH, mcpServerPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";
import { emptyFake, LINK, LINK_SHA, linkResolution, mockMcp, record, type Fake } from "./mcpFixture";

test.setTimeout(60_000);

const DISCLAIMER = "Installs the published release; not built from this commit.";
const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

async function toReview(page: Page, f: Fake, link = LINK) {
  await mockMcp(page, f);
  await page.goto(MCP_NEW_PATH);
  await expect(page.getByTestId("mcp-step-source")).toBeVisible();
  const tile = page.getByTestId("mcp-source-kinds").locator("label").filter({ hasText: "Repository link" });
  await expect(page.getByRole("radio", { name: /Git repository/ })).toHaveCount(0);
  await page.getByRole("radio", { name: /Repository link/ }).check();
  // Selection is said in words on the picked tile, not by colour alone.
  await expect(tile.getByTestId("mcp-kind-selected")).toHaveText("Selected");
  await expect(page.getByTestId("mcp-kind-selected")).toHaveCount(1);
  if (isPhone(page)) {
    const box = (await tile.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByRole("textbox", { name: "Repository link" }).fill(link);
  await page.getByRole("button", { name: "Next: Review" }).click();
  await expect(page.getByTestId("mcp-step-review")).toBeVisible();
}

const creates = (f: Fake) => f.calls.filter((c) => c.method === "POST" && c.path === "/api/mcp/servers");

test("a resolved link: where it comes from, the disclaimer, P1b's facts; Install registers by resolution_id", async ({
  page,
}) => {
  const f = emptyFake();
  f.opPolls = 4;
  f.links[LINK] = { resolution: linkResolution() };
  await toReview(page, f);
  // Reading: progress rows, Stop reading, and the footer's Cancel — nothing is registered yet.
  await expect(page.getByTestId("mcp-resolve-rows")).toBeVisible();
  await expect(page.getByTestId("mcp-resolve-stop")).toBeVisible();
  await expect(page.getByRole("link", { name: "Cancel" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Finish later" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Install" })).toBeDisabled();
  await expect(page.getByTestId("mcp-resolve-rows").locator("li[data-state=done]").first()).toContainText(
    "default branch head 3f2a9c1",
  );

  const origin = page.getByTestId("mcp-review-origin");
  await expect(origin).toBeVisible();
  await expect(origin).toContainText("github.com/owner/repo @ 3f2a9c1");
  await expect(origin).toContainText(LINK_SHA);
  await expect(origin).toContainText("package.json → npm @acme/mcp-server 1.4.0");
  await expect(origin).toContainText("names github.com/owner/repo");
  await expect(page.getByTestId("mcp-origin-disclaimer")).toHaveText(DISCLAIMER);
  await expect(page.getByTestId("mcp-review-facts")).toContainText("@acme/mcp-server");
  await expect(page.getByText("No package has been downloaded yet: Install fetches exactly this lock.")).toBeVisible();
  // The disclaimer sits between the repository facts and the release facts.
  const [d, r] = await Promise.all([
    page.getByTestId("mcp-origin-disclaimer").boundingBox(),
    page.getByTestId("mcp-review-facts").boundingBox(),
  ]);
  expect(d!.y).toBeLessThan(r!.y);
  await expect(page.getByRole("link", { name: "Cancel" })).toBeVisible();
  expect(creates(f)).toEqual([]); // resolving registered nothing

  if (isPhone(page)) {
    const box = (await page.getByRole("button", { name: "Install" }).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.getByRole("button", { name: "Install" }).click();
  await expect(page.getByTestId("mcp-step-settings")).toBeVisible();
  const [create] = creates(f);
  const body = create.body as Record<string, unknown>;
  const resolve = f.calls.find((c) => c.path === "/api/mcp/servers/resolve-link")!;
  expect(body.resolution_id).toBe((resolve.body as { request_id: string }).request_id);
  expect(body.source).toEqual(linkResolution().source);
  expect(body).not.toHaveProperty("origin");
  expect(body.name).toBe("repo");
  // Exactly the lock Review showed is installed.
  const install = f.calls.find((c) => c.path.endsWith("/install"))!;
  expect((install.body as { review_id: string }).review_id).toBe("a1b2c3d4e5f60718");
  expect(f.servers[0].origin).toEqual({ url: LINK, commit: LINK_SHA });
  await expect(page.getByRole("link", { name: "Finish later" })).toBeVisible();
});

test("a refused link (both manifests): amber words, what was read, no Install; Try another link goes back", async ({
  page,
}) => {
  const f = emptyFake();
  f.links[LINK] = {
    error: "this repository declares both an npm and a Python package. BattleLab never picks one for you, so nothing was registered",
    details: {
      repository: "github.com/owner/repo",
      commit: LINK_SHA,
      found: [
        { manifest: "package.json", kind: "npm", package: "@acme/mcp-server", version: "1.4.0" },
        { manifest: "pyproject.toml", kind: "pypi", package: "acme-mcp", version: "1.4.0" },
      ],
    },
  };
  await toReview(page, f);
  const refused = page.getByTestId("mcp-link-refused");
  await expect(refused).toContainText("Can’t add this repository");
  await expect(refused).toContainText("This repository declares both an npm and a Python package.");
  await expect(refused).toContainText("github.com/owner/repo @ 3f2a9c1");
  await expect(refused).toContainText("pyproject.toml → PyPI acme-mcp 1.4.0");
  // The warn tone, never the red of a failure.
  const border = await refused.evaluate((el) => getComputedStyle(el).borderLeftColor);
  const warn = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.color = "var(--status-degraded)";
    document.body.append(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  });
  expect(border).toBe(warn);
  await expect(page.getByText(/choose npm package or PyPI package on Source/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Cancel" })).toBeVisible();
  await refused.getByRole("button", { name: "Try another link" }).click();
  await expect(page.getByTestId("mcp-step-source")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Repository link" })).toHaveValue(LINK);
  expect(creates(f)).toEqual([]);
});

test("an unpublished repository is refused in words: nothing is built", async ({ page }) => {
  const f = emptyFake();
  f.links[LINK] = {
    error:
      "npm has no published release @acme/mcp-server 1.4.0. BattleLab installs only a published release and never builds one from the repository's code, so nothing was registered",
    details: { repository: "github.com/owner/repo", commit: LINK_SHA, found: [] },
  };
  await toReview(page, f);
  const refused = page.getByTestId("mcp-link-refused");
  await expect(refused).toContainText("npm has no published release @acme/mcp-server 1.4.0");
  await expect(refused).toContainText("never builds one");
  await expect(page.getByText(/installs only what the registry publishes/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Install" })).toHaveCount(0);
  expect(creates(f)).toEqual([]);
});

test("a /tree/ link is refused before anything is read; Stop reading ends a read", async ({ page }) => {
  const f = emptyFake();
  f.opPolls = Infinity;
  f.links[LINK] = { resolution: linkResolution() };
  await mockMcp(page, f);
  await page.goto(MCP_NEW_PATH);
  await page.getByRole("radio", { name: /Repository link/ }).check();
  await page.getByRole("textbox", { name: "Repository link" }).fill(`${LINK}/tree/main/packages/server`);
  await page.getByRole("button", { name: "Next: Review" }).click();
  await expect(page.getByText("Link the repository itself — no /tree/ or /blob/ links.")).toBeVisible();
  expect(f.calls.filter((c) => c.path.endsWith("/resolve-link"))).toEqual([]);

  await page.getByRole("textbox", { name: "Repository link" }).fill(LINK);
  await page.getByRole("button", { name: "Next: Review" }).click();
  await expect(page.getByTestId("mcp-resolve-rows")).toBeVisible();
  await page.getByTestId("mcp-resolve-stop").click();
  await expect(page.getByTestId("mcp-step-source")).toBeVisible();
  await expect.poll(() => f.calls.some((c) => c.path.endsWith("/stop"))).toBe(true);
  expect(creates(f)).toEqual([]);
});

test("the server page's Settings → Source shows the origin, display only", async ({ page }) => {
  const f = emptyFake();
  const s = record({
    name: "acme",
    label: "Acme",
    source: { kind: "npm", package: "@acme/mcp-server", version: "1.4.0" },
    transport: "stdio",
    origin: { url: LINK, commit: LINK_SHA },
  });
  f.servers.push(s);
  await mockMcp(page, f);
  await page.goto(`${mcpServerPath(s.id)}?tab=settings`);
  const origin = page.getByTestId("mcp-source-origin");
  await expect(origin).toContainText("@acme/mcp-server · npm");
  await expect(origin).toContainText("From a repository · github.com/owner/repo @ 3f2a9c1");
  await expect(origin).toContainText(DISCLAIMER);
  await expect(origin).toContainText("Saving a different source — kind, package or version — removes this line.");
  await expect(origin.locator("input")).toHaveCount(0); // nothing here is editable
});
