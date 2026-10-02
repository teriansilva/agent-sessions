import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import type { PluginCatalog, PluginGeneration, PluginOperation, PluginReview } from "../src/types/plugins";

const PATH = "/settings/agents/setup/new";
const GEN = "c0ef9b0a-322c-49c2-843e-d8c7564918c2";
const REVIEW: PluginReview = {
  id: "e51cddbe-23e6-42bf-a994-51e08e9b087f", plugin_id: "sample-agent", source: "local", sequence: null,
  digest: "a".repeat(64), recipe_digest: "b".repeat(64), adopted_path: null, adopted_sha256: null,
  expires_at: 2_000_000_000,
  entry: { manifest: { identity: { id: "sample-agent", label: "Sample agent", version: "1.2.3", publisher: "Local publisher" },
    runtime: { kind: "pty" }, install: { kind: "tarball" }, signin: { kind: "cli-subcommand", subcommand: "login" } },
    recipe: { artifacts: [{ url: `https://github.com/example/sample/releases/download/v1.2.3/${"long-name-".repeat(10)}.tar.gz`, sha256: "c".repeat(64), destination: "." }] } },
};
function generation(): PluginGeneration {
  return { id: GEN, review: structuredClone(REVIEW), required_checks: ["binary", "version", "new", "resume", "transcript", "usage"], verification: null };
}

async function fixture(page: Page, staged = false) {
  const gen = generation();
  const data: PluginCatalog = { feed: { state: "ready", sequence: 9, error: null },
    catalog: [{ manifest: REVIEW.entry.manifest, digest: REVIEW.digest }],
    plugins: staged ? [{ id: REVIEW.plugin_id, active: null, candidate: GEN, enabled: false, generations: [gen] }] : [],
    operations: [], roster_generation: 1, roster_revision: null };
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const sockets: string[] = [];
  const sent: string[] = [];
  let verifyPass = true;
  let installState: PluginOperation["state"] = "installed";
  let loseResponse = false;
  const rejected = new Map<string, string>();
  const lost = new Set<string>();
  const neverReceived = new Set<string>();
  const op = (id: string, kind: PluginOperation["kind"], state: PluginOperation["state"]): PluginOperation => ({
    id, plugin_id: REVIEW.plugin_id, kind, state, created_at: 1, updated_at: 1,
    generation_id: kind === "install" ? id : GEN, review: kind === "install" ? gen.review : null,
    review_digest: gen.review.digest, error: null,
  });
  await page.route("**/api/**", r => r.fulfill({ json: {} }));
  await page.route("**/api/config", r => r.fulfill({ json: {
    csrf: "test-csrf", new_session_engines: [], terminal_backend: "ws", auth_mode: "none",
    agent_defaults: { default_engine: "missing-saved-default", bypass: false },
  } }));
  await mockRoster(page);
  await page.route("**/api/sessions**", r => r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route(/\/api\/projects(\?.*)?$/, r => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, r => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/missions**", r => r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/pulse/notifications**", r => r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));

  await page.route("**/api/agents/usage", r => r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/plugins**", async r => {
    const path = new URL(r.request().url()).pathname;
    if (r.request().method() === "GET") {
      if (path === "/api/plugins") return r.fulfill({ json: data });
      const found = data.operations.find(o => path.endsWith(o.id));
      return r.fulfill({ status: found ? 200 : 409, json: found ?? { detail: "plugin operation not found" } });
    }
    const body = r.request().postDataJSON() as Record<string, unknown>;
    writes.push({ path, body });
    if (path.endsWith("/review")) return r.fulfill({ json: { ...gen.review, source: body.local ? "local" : "signed" } });
    if (path.endsWith("/refresh")) return r.fulfill({ json: data });
    if (path.endsWith("/reload")) return r.fulfill({ json: { generation: 2 } });
    if (path.endsWith("/cancel")) {
      const found = data.operations.find(o => path.includes(o.id))!; found.state = "interrupted";
      return r.fulfill({ json: found });
    }
    const kind = path.split("/").at(-1) as PluginOperation["kind"];
    if (neverReceived.delete(kind)) return r.abort();
    if (rejected.has(kind)) {
      const detail = rejected.get(kind); rejected.delete(kind);
      return r.fulfill({ status: 409, json: { detail } });
    }
    if (["disable", "remove"].includes(kind) && body.expected_revision !== data.roster_revision)
      return r.fulfill({ status: 409, json: { detail: "the agent roster changed; refresh and confirm again" } });
    const id = body.request_id as string;
    const result = op(id, kind, kind === "install" ? installState : kind === "signin" ? "ready" : kind === "verify" ? verifyPass ? "verified" : "failed" : "complete");
    if (kind === "install") {
      gen.id = id; result.generation_id = id;
      data.plugins = [{ id: REVIEW.plugin_id, active: null, candidate: id, enabled: false, generations: [gen] }];
    } else result.generation_id = body.generation_id as string;
    if (kind === "verify") gen.verification = { digest: gen.review.digest, checked_at: 2,
      results: gen.required_checks.map(check => ({ check, passed: verifyPass || check !== "resume", detail: verifyPass ? "Real check passed." : "Vendor account is unavailable." })) };
    if (kind === "activate") { data.plugins[0].enabled = true; data.plugins[0].active = gen.id; }
    if (kind === "remove" || kind === "disable") data.plugins[0].enabled = false;
    if (["activate", "disable", "remove"].includes(kind)) data.roster_revision = id;
    data.operations.push(result);
    if (kind === "install" && loseResponse || lost.delete(kind)) return r.abort();
    return r.fulfill({ json: result });
  });
  await page.routeWebSocket(/\/ws\/plugins\/signin\//, ws => {
    sockets.push(ws.url());
    const current = data.operations.find(o => ws.url().endsWith(o.id))!;
    current.state = "running";
    ws.send(Buffer.from("Sign in: ephemeral-secret-marker\r\n"));
    ws.onMessage(msg => sent.push(typeof msg === "string" ? msg : new TextDecoder().decode(msg as ArrayBuffer)));
    ws.onClose(() => { current.state = "interrupted"; });
  });
  page.on("dialog", dialog => void dialog.accept());
  return { data, gen, writes, sockets, sent, dropNext: (kind: string) => neverReceived.add(kind), rejectNext: (kind: string, detail: string) => rejected.set(kind, detail), loseNext: (kind: string) => lost.add(kind), setVerify: (value: boolean) => { verifyPass = value; },
    setInstall: (value: PluginOperation["state"], lost = false) => { installState = value; loseResponse = lost; } };
}
const posts = (m: Awaited<ReturnType<typeof fixture>>, suffix: string) => m.writes.filter(w => w.path.endsWith(suffix));
async function localReview(page: Page) {
  await page.goto(PATH);
  await page.getByRole("button", { name: "Local manifest", exact: true }).click();
  await page.getByLabel("Manifest and recipe JSON").fill(JSON.stringify(REVIEW.entry));
  await page.getByRole("button", { name: "Review installation" }).click();
  await expect(page.getByRole("heading", { name: "Review this installation" })).toBeVisible();
}
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test("gallery adds an agent without changing the live roster or defaults", async ({ page }) => {
  const m = await fixture(page);
  await page.goto("/settings/agents");
  await page.getByLabel("Search agents").fill("sample");
  await expect(page.getByRole("heading", { name: "Sample agent" })).toBeVisible();
  await page.getByRole("button", { name: "Ready", exact: true }).click();
  await expect(page.getByText("No agents match these filters.")).toBeVisible();
  await page.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("link", { name: "Set up agent" }).click();
  await expect(page.getByRole("combobox", { name: "Agent", exact: true })).toHaveValue("sample-agent");
  expect(m.writes).toEqual([]);
  await noOverflow(page);
});

test("local review needs fresh consent and exact source stays untrusted after verification", async ({ page }) => {
  const m = await fixture(page);
  await localReview(page);
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeDisabled();
  await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Review installation" }).click();
  await expect(page.getByRole("checkbox", { name: /I trust this exact/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
  expect(posts(m, "/install")).toHaveLength(1);
  expect(posts(m, "/install")[0].body).toMatchObject({ digest: REVIEW.digest, confirm_local: true });
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await expect(page.getByRole("button", { name: "Enable agent" })).toHaveCount(0);
  await page.getByRole("button", { name: "Verify installation" }).click();
  await expect(page.getByText(/Its source remains local and untrusted/)).toBeVisible();
  expect(posts(m, "/verify")[0].body).toMatchObject({ confirm_effects: true, generation_id: m.gen.id });
  await page.getByRole("button", { name: "Enable agent" }).click();
  await expect(page.getByText(/Agent enabled. Your saved defaults/)).toBeVisible();
  expect(posts(m, "/activate")).toHaveLength(1);
  expect(posts(m, "/reload")).toHaveLength(1);
  await noOverflow(page);
});

test("lost install response and page reload read the same operation without replay", async ({ page }) => {
  const m = await fixture(page); m.setInstall("running", true);
  await localReview(page);
  await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.getByRole("button", { name: "Check operation status" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "install · running" })).toBeVisible();
  expect(posts(m, "/install")).toHaveLength(1);
  await page.getByRole("link", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("server operation continues");
  await page.getByRole("button", { name: "Leave setup", exact: true }).click();
  await page.getByRole("link", { name: "View operation" }).click();
  await expect(page.getByRole("heading", { name: "install · running" })).toBeVisible();
  m.data.operations[0].state = "installed";
  await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
  expect(posts(m, "/install")).toHaveLength(1);
});

test("failed verification cannot enable and reload pins an older generation", async ({ page }) => {
  const m = await fixture(page, true); m.setVerify(false);
  m.data.plugins[0].generations!.push({ ...generation(), id: "newer-candidate" });
  m.data.plugins[0].candidate = "newer-candidate";
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await page.getByRole("button", { name: "Verify installation" }).click();
  await expect(page.getByRole("heading", { name: "verify · failed" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Enable agent" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "verify · failed" })).toBeVisible();
  expect(posts(m, "/verify")[0].body.generation_id).toBe(GEN);
  expect(posts(m, "/verify")).toHaveLength(1);
  expect(posts(m, "/activate")).toHaveLength(0);
});

for (const signinKind of ["cli-subcommand", "auth-login", "interactive"]) {
test(`temporary ${signinKind} sign-in opens once and closing it never reconnects or persists terminal bytes`, async ({ page }) => {
  const m = await fixture(page, true);
  m.gen.review.entry.manifest.signin = { kind: signinKind };
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await page.getByRole("button", { name: "Start sign-in", exact: true }).click();
  await expect.poll(() => m.sockets.length).toBe(1);
  await page.getByRole("button", { name: "Close sign-in terminal" }).click();
  await expect(page.getByRole("heading", { name: "signin · interrupted" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "signin · interrupted" })).toBeVisible();
  expect(m.sockets).toHaveLength(1);
  expect(posts(m, "/signin")).toHaveLength(1);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("ephemeral-secret-marker");
});
}

test("the sign-in terminal forwards a pasted vendor code as terminal input", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const m = await fixture(page, true);
  m.gen.review.entry.manifest.signin = { kind: "cli-subcommand", subcommand: "login" };
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await page.getByRole("button", { name: "Start sign-in", exact: true }).click();
  await expect.poll(() => m.sockets.length).toBe(1);
  await page.getByLabel("Temporary vendor sign-in terminal").click();
  await page.evaluate(() => navigator.clipboard.writeText("oauth-code-123"));
  await page.keyboard.press("Control+V");
  await expect.poll(() => m.sent.join("")).toContain("oauth-code-123");
});

test("remove confirmation preserves data and Escape returns focus", async ({ page }) => {
  const m = await fixture(page, true);
  await page.goto("/settings/agents");
  await page.getByLabel("Search agents").fill("sample");
  const remove = page.getByRole("button", { name: "Remove", exact: true });
  await remove.click();
  await expect(page.getByRole("dialog")).toContainText("Vendor transcripts, credentials and installed copies stay");
  await page.keyboard.press("Escape");
  await expect(remove).toBeFocused();
  expect(posts(m, "/remove")).toHaveLength(0);
  await remove.click();
  await page.getByRole("button", { name: "Remove agent", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(posts(m, "/remove")).toHaveLength(1);
  expect(posts(m, "/reload")).toHaveLength(0); // The mutation already commits its roster revision.
});

for (const theme of ["dark", "light"]) {
  test(`gallery and setup visual states (${theme})`, async ({ page }, info) => {
    const m = await fixture(page, true);
    await page.addInitScript(value => localStorage.setItem("tr-theme", value), theme);
    await page.emulateMedia({ reducedMotion: "reduce" });
    const shot = async (state: string) => {
      await noOverflow(page);
      await page.screenshot({ path: `../design-review/853-${state}-${info.project.name}-${theme}.png`, fullPage: true });
    };
    await page.goto("/settings/agents");
    await page.getByLabel("Search agents").fill("sample");
    await expect(page.getByRole("heading", { name: "Sample agent" })).toBeVisible();
    await shot("gallery");
    await page.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await shot("remove");
    await page.keyboard.press("Escape");
    await page.getByLabel("Search agents").fill("nothing-matches");
    await expect(page.getByText("No agents match these filters.")).toBeVisible();
    await shot("empty");
    await localReview(page);
    await page.getByText("Source and pinned digests").click();
    await shot("review");
    await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
    m.setInstall("running");
    await page.getByRole("button", { name: "Install", exact: true }).click();
    await expect(page.getByRole("heading", { name: "install · running" })).toBeVisible();
    await shot("running");
    m.data.operations[0].state = "installed";
    await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
    await page.getByRole("button", { name: "Start sign-in", exact: true }).click();
    await expect.poll(() => m.sockets.length).toBe(1);
    await shot("signin");
    await page.getByRole("button", { name: "Close sign-in terminal" }).click();
    await expect(page.getByRole("heading", { name: "signin · interrupted" })).toBeVisible();
    await page.getByRole("button", { name: "Continue to verification" }).click();
    m.setVerify(false);
    await page.getByRole("button", { name: "Verify installation" }).click();
    await expect(page.getByRole("heading", { name: "verify · failed" })).toBeVisible();
    await shot("failure");
    m.setVerify(true);
    await page.getByRole("button", { name: "Run verification again" }).click();
    await expect(page.getByRole("button", { name: "Enable agent" })).toBeVisible();
    await shot("verify");
  });
}

test("updates compare the signed recipe, not the operator confirmation digest", async ({ page }) => {
  const m = await fixture(page, true);
  m.data.plugins[0].active = GEN;
  m.data.plugins[0].enabled = true;
  m.data.catalog[0].digest = m.gen.review.recipe_digest;
  await page.goto("/settings/agents");
  await page.getByLabel("Search agents").fill("sample");
  await page.getByRole("button", { name: "Updates", exact: true }).click();
  await expect(page.getByText("No agents match these filters.")).toBeVisible();
  m.data.catalog[0].digest = "d".repeat(64);
  await page.getByRole("button", { name: "Refresh catalog" }).click();
  await expect(page.getByRole("link", { name: "Review update" })).toBeVisible();
});

test("setup controls retain touch size, keyboard order and wrapped sources at 320px", async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 320, height: 740 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/settings/agents");
  const targets = [page.getByLabel("Search agents"), page.getByRole("button", { name: "Refresh catalog" }),
    page.getByRole("link", { name: "Add agent" }), page.getByRole("button", { name: "All", exact: true })];
  for (const target of targets) {
    const box = await target.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.width).toBeGreaterThanOrEqual(44);
  }
  await page.getByLabel("Search agents").focus();
  for (const name of ["All", "Ready", "Needs setup", "Updates", "Disabled"]) {
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name, exact: true })).toBeFocused();
  }
  await noOverflow(page);
  await localReview(page);
  await expect(page.getByRole("heading", { name: "Review this installation" })).toBeFocused();
  await page.keyboard.press("Tab");
  const details = page.getByText("Source and pinned digests", { exact: true });
  await expect(details).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  const consent = page.getByRole("checkbox", { name: /I trust this exact/ });
  await expect(consent).toBeFocused();
  const label = consent.locator("..");
  expect((await label.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await page.keyboard.press("Space");
  await expect(consent).toBeChecked();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Cancel", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Back", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeFocused();
  const panel = page.getByRole("region", { name: "Reviewed installation" });
  expect(await panel.locator("p, dd").evaluateAll(nodes => nodes.every(n => n.scrollWidth <= n.clientWidth + 1))).toBe(true);
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  const progress = page.getByTestId("wizard-progress");
  expect(await progress.evaluate(node => [node, ...node.querySelectorAll("*")].every(n => {
    const style = getComputedStyle(n);
    return style.animationName === "none" && style.transitionDuration.split(",").every(v => parseFloat(v) <= .01);
  }))).toBe(true);
  await noOverflow(page);
});


test("definite install rejection returns to a fresh review while a lost response retains its ID", async ({ page }) => {
  const m = await fixture(page);
  m.rejectNext("install", "The review expired; review this installation again.");
  await localReview(page);
  await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Choose an agent" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Check operation status" })).toHaveCount(0);
  await page.getByRole("button", { name: "Review installation" }).click();
  await expect(page.getByRole("checkbox", { name: /I trust this exact/ })).not.toBeChecked();
  await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
  expect(posts(m, "/install")).toHaveLength(2);
});

test("a rejected verification can be retried without leaving setup", async ({ page }) => {
  const m = await fixture(page, true);
  m.rejectNext("verify", "another plugin operation is busy");
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await page.getByRole("button", { name: "Verify installation" }).click();
  await expect(page.getByRole("button", { name: "Check operation status" })).toHaveCount(0);
  // Reconciled generation is still unverified and can be prepared again.
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await page.getByRole("button", { name: "Verify installation" }).click();
  await expect(page.getByRole("button", { name: "Enable agent" })).toBeVisible();
  expect(posts(m, "/verify")).toHaveLength(2);
});

test("a lost remove response survives reload and cannot remove an intervening activation", async ({ page }) => {
  const m = await fixture(page, true);
  m.data.plugins[0].enabled = true; m.data.plugins[0].active = GEN;
  m.loseNext("remove");
  await page.goto("/settings/agents");
  const card = page.locator('[data-plugin-id="sample-agent"]');
  await card.getByRole("button", { name: "Remove", exact: true }).click();
  await page.getByRole("button", { name: "Remove agent", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  m.data.plugins[0].enabled = true;
  m.data.plugins[0].active = "another-activation";
  await page.reload();
  await card.getByRole("button", { name: "Check previous remove" }).click();
  await page.getByRole("button", { name: "Remove agent", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(posts(m, "/remove")).toHaveLength(1);
  expect(m.data.plugins[0].enabled).toBe(true);
  expect(m.data.plugins[0].active).toBe("another-activation");
});

for (const kind of ["disable", "remove"] as const) test(`a delayed ${kind} cannot undo re-enabling the same installation`, async ({ page }) => {
  const m = await fixture(page, true);
  m.data.plugins[0].enabled = true; m.data.plugins[0].active = GEN;
  await page.goto("/settings/agents");
  const card = page.locator('[data-plugin-id="sample-agent"]');
  const label = kind === "remove" ? "Remove" : "Disable";
  await card.getByRole("button", { name: label, exact: true }).click();
  // Another operator disables and re-enables G1 while this confirmation stays open.
  const revision = "d6ba9ee1-9eec-4cc2-9133-d125fe4576a2";
  m.data.roster_revision = revision;
  await page.getByRole("button", { name: `${label} agent`, exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("confirm a new decision");
  expect(m.data.plugins[0].enabled).toBe(true);
  expect(m.data.operations).toHaveLength(0);
  const previous = posts(m, `/${kind}`)[0].body;
  expect(previous.expected_active).toBe(GEN);
  expect(previous.expected_revision).toBeNull();
  await card.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("button", { name: `${label} agent`, exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(m.data.plugins[0].enabled).toBe(false);
  const fresh = posts(m, `/${kind}`)[1].body;
  expect(fresh.expected_active).toBe(GEN);
  expect(fresh.expected_revision).toBe(revision);
  expect(fresh.request_id).not.toBe(previous.request_id);
});

for (const kind of ["disable", "remove"] as const) test(`a missing ${kind} requires a refreshed new confirmation`, async ({ page }) => {
  const m = await fixture(page, true);
  m.data.plugins[0].enabled = true; m.data.plugins[0].active = GEN;
  m.dropNext(kind);
  await page.goto("/settings/agents");
  const card = page.locator('[data-plugin-id="sample-agent"]');
  const label = kind === "remove" ? "Remove" : "Disable";
  await card.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("button", { name: `${label} agent`, exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  const previous = posts(m, `/${kind}`)[0].body;
  expect(previous.expected_active).toBe(GEN);
  expect(m.data.operations).toHaveLength(0);
  m.data.plugins[0].active = "another-activation";
  await page.reload();
  await card.getByRole("button", { name: `Check previous ${kind}` }).click();
  await page.getByRole("button", { name: `${label} agent`, exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("confirm a new decision");
  await expect(card.getByRole("button", { name: `Check previous ${kind}` })).toHaveCount(0);
  expect(posts(m, `/${kind}`)).toHaveLength(1);
  expect(m.data.plugins[0].enabled).toBe(true);
  await card.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("button", { name: `${label} agent`, exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const fresh = posts(m, `/${kind}`)[1].body;
  expect(fresh.expected_active).toBe("another-activation");
  expect(fresh.request_id).not.toBe(previous.request_id);
});

test("saving a verified candidate endpoint clears the verdict before enable", async ({ page }) => {
  const m = await fixture(page, true);
  m.gen.review.entry.manifest.runtime = { kind: "chat" };
  m.gen.required_checks = ["endpoint"];
  let ep = { base_url: "https://one.test/v1", model: "sample", api_key_set: true, configured: true,
    context_window: 32768, max_output_tokens: 4096, request_timeout: null, tools: "none" };
  await page.route("**/generations/*/endpoint", r => {
    if (r.request().method() === "PATCH") {
      const patch = r.request().postDataJSON(); delete patch.api_key;
      ep = { ...ep, ...patch }; m.gen.verification = null;
    }
    return r.fulfill({ json: ep });
  });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await page.getByRole("button", { name: "Verify installation" }).click();
  await expect(page.getByRole("button", { name: "Enable agent" })).toBeVisible();
  await page.getByRole("button", { name: "Back to account setup" }).click();
  await page.getByLabel("Base URL").fill("https://two.test/v1");
  await page.getByLabel("API key", { exact: true }).fill("a-new-test-key");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved — run verification before enabling this installation.")).toBeVisible();
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await expect(page.getByRole("button", { name: "Enable agent" })).toHaveCount(0);
  await expect(page.getByText("All required checks passed", { exact: false })).toHaveCount(0);
});


for (const staged of [false, true]) {
  test(`confirmed missing operation on reload recovers ${staged ? "the candidate" : "installation review"}`, async ({ page }) => {
    const m = await fixture(page, staged);
    const query = new URLSearchParams({ plugin: "sample-agent", operation: crypto.randomUUID(), ...(staged ? { generation: GEN } : {}) });
    await page.goto(`${PATH}?${query}`);
    await expect(page.getByRole("button", { name: "Check operation status" })).toHaveCount(0);
    await expect.poll(() => new URL(page.url()).searchParams.has("operation")).toBe(false);
    await expect(page.getByRole("button", { name: staged ? "Continue to verification" : "Review installation" })).toBeEnabled();
    expect(new URL(page.url()).searchParams.get("generation")).toBe(staged ? GEN : null);
    expect(m.writes).toHaveLength(0);
  });
}

for (const kind of ["install", "verify"] as const) {
  test(`ambiguous ${kind} retains its ID until status confirms a missing operation`, async ({ page }) => {
    await fixture(page, kind === "verify");
    const attempts: Record<string, unknown>[] = [];
    let confirmedMissing = false;
    await page.route(`**/api/plugins/${kind}`, r => {
      attempts.push(r.request().postDataJSON());
      return attempts.length === 1 ? r.abort() : r.fallback();
    });
    await page.route("**/api/plugins/operations/*", r => {
      if (!attempts.length || !r.request().url().endsWith(attempts[0].request_id as string)) return r.fallback();
      return r.fulfill({ status: confirmedMissing ? 409 : 503,
        json: { detail: confirmedMissing ? "plugin operation not found" : "temporarily unavailable" } });
    });
    if (kind === "install") {
      await localReview(page);
      await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
      await page.getByRole("button", { name: "Install", exact: true }).click();
    } else {
      await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
      await page.getByRole("button", { name: "Continue to verification" }).click();
      await page.getByRole("button", { name: "Verify installation" }).click();
    }
    const check = page.getByRole("button", { name: "Check operation status" });
    await expect(check).toBeEnabled();
    const first = new URL(page.url()).searchParams.get("operation");
    await check.click();
    await expect(page.getByRole("alert")).toContainText("temporarily unavailable");
    expect(new URL(page.url()).searchParams.get("operation")).toBe(first);
    await expect(check).toBeEnabled();
    confirmedMissing = true;
    await check.click();
    await expect(check).toHaveCount(0);
    expect(new URL(page.url()).searchParams.has("operation")).toBe(false);
    expect(attempts).toHaveLength(1);
    if (kind === "install") {
      await page.getByRole("button", { name: "Review installation" }).click();
      await expect(page.getByRole("checkbox", { name: /I trust this exact/ })).not.toBeChecked();
      await page.getByRole("checkbox", { name: /I trust this exact/ }).check();
      await page.getByRole("button", { name: "Install", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Connect your account" })).toBeVisible();
    } else {
      expect(new URL(page.url()).searchParams.get("generation")).toBe(GEN);
      await page.getByRole("button", { name: "Continue to verification" }).click();
      await page.getByRole("button", { name: "Verify installation" }).click();
      await expect(page.getByRole("button", { name: "Enable agent" })).toBeVisible();
      expect(attempts[1].generation_id).toBe(GEN);
    }
    expect(attempts).toHaveLength(2);
    expect(attempts[1].request_id).not.toBe(attempts[0].request_id);
  });
}

for (const theme of ["dark", "light"]) {
  test(`agent detail manages active and candidate installations (${theme})`, async ({ page }, info) => {
    const m = await fixture(page, true);
    const active = structuredClone(m.gen);
    active.id = "bc29a026-9eb7-4b71-986b-e66c075cbdb7";
    active.review.entry.manifest.identity.version = "1.0.0";
    active.verification = { digest: active.review.digest, checked_at: 1790852400,
      results: active.required_checks.map(check => ({ check, passed: true, detail: "Existing installation passed." })) };
    Object.assign(m.data.plugins[0], { active: active.id, enabled: true, generations: [active, m.gen] });
    await page.addInitScript(value => localStorage.setItem("tr-theme", value), theme);
    await page.route("**/api/engines/sample-agent", r => r.fulfill({ json: {
      id: "sample-agent", label: "Sample agent", publisher: "Local publisher", version: "1.0.0", contract: 1,
      source: "managed", kind: "agent", runtime: "pty", binary: { name: "sample-agent", search_paths: [] },
      provenance: { state: "local", via: "install", path: "/private/sample-agent", note: null },
      store: null, launch: null, transcript: { kind: "none" }, usage: { source: "none" },
      capabilities: {}, models: [], maintenance: [],
    } }));
    await page.goto("/settings/agents/sample-agent");
    const management = page.getByRole("region", { name: "Installation management" });
    await expect(management.getByRole("region", { name: "Active installation" })).toContainText("1.0.0");
    const activePanel = management.getByRole("region", { name: "Active installation" });
    await activePanel.locator("summary").click();
    await expect(activePanel.getByText("Existing installation passed.").first()).toBeVisible();
    await activePanel.locator("summary").click();
    await expect(management.getByRole("region", { name: "Candidate installation" })).toContainText("1.2.3");
    const candidatePanel = management.getByRole("region", { name: "Candidate installation" });
    await candidatePanel.locator("summary").click();
    await expect(candidatePanel.getByText("Pending", { exact: true }).first()).toBeVisible();
    await candidatePanel.locator("summary").click();
    await expect(management).toContainText("Signed catalog · sequence 9");
    await expect(management.getByRole("link", { name: "Review update" })).toBeVisible();
    expect(m.writes).toHaveLength(0);
    for (const control of await management.locator("button, a").all()) {
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      expect((await control.boundingBox())!.height).toBeLessThanOrEqual(60);
    }
    await noOverflow(page);
    await activePanel.screenshot({ path: `../design-review/853-detail-active-${info.project.name}-${theme}.png` });
    await candidatePanel.screenshot({ path: `../design-review/853-detail-candidate-${info.project.name}-${theme}.png` });
    await management.getByRole("link", { name: "Continue setup" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `../design-review/853-detail-${info.project.name}-${theme}.png` });
    await management.getByRole("button", { name: "Refresh catalog" }).click();
    await expect.poll(() => posts(m, "/refresh").length).toBe(1);
    await management.getByRole("button", { name: "Reload agent roster" }).click();
    await expect.poll(() => posts(m, "/reload").length).toBe(1);
    await management.getByRole("button", { name: "Disable", exact: true }).click();
    await page.getByRole("button", { name: "Disable agent", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(posts(m, "/disable")).toHaveLength(1);
    await management.getByRole("button", { name: "Remove", exact: true }).click();
    await page.keyboard.press("Escape");
    expect(posts(m, "/remove")).toHaveLength(0);
    await management.getByRole("link", { name: "Continue setup" }).click();
    await expect(page.getByRole("button", { name: "Continue to verification" })).toBeVisible();
    expect(new URL(page.url()).searchParams.get("generation")).toBe(GEN);
  });
}

test("agent detail retains a saved candidate when no live engine exists", async ({ page }) => {
  await fixture(page, true);
  await page.route("**/api/engines/sample-agent", r => r.fulfill({ status: 404, json: { detail: "unknown engine" } }));
  await page.goto("/settings/agents");
  await page.getByRole("link", { name: "Details for Sample agent" }).click();
  const management = page.getByRole("region", { name: "Installation management" });
  await expect(management.getByRole("region", { name: "Candidate installation" })).toContainText("1.2.3");
  await expect(management.getByRole("button", { name: "Remove", exact: true })).toBeVisible();
  await expect(management.getByRole("link", { name: "Continue setup" })).toBeVisible();
});

test("an activated API endpoint stays read-only after disabling", async ({ page }) => {
  const m = await fixture(page, true);
  m.gen.review.entry.manifest.runtime = { kind: "chat" };
  m.gen.required_checks = ["endpoint"];
  Object.assign(m.data.plugins[0], { active: GEN, enabled: false });
  await page.route("**/generations/*/endpoint", r => r.fulfill({ json: {
    base_url: "https://saved.test/v1", model: "saved-model", api_key_set: true, configured: true,
    context_window: 32768, max_output_tokens: 4096, request_timeout: null, tools: "write",
  } }));
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await expect(page.getByText("https://saved.test/v1 · saved-model")).toBeVisible();
  await expect(page.getByText("This installation’s endpoint is fixed after activation.", { exact: false })).toBeVisible();
  await expect(page.getByLabel("Base URL")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Continue to verification" }).click();
  await expect(page.getByRole("button", { name: "Verify installation" })).toBeVisible();
  expect(m.writes).toHaveLength(0);
});

test("reopening an enabled installation does not offer re-verification", async ({ page }) => {
  const m = await fixture(page, true);
  m.gen.verification = { digest: m.gen.review.digest, checked_at: 1790852400,
    results: m.gen.required_checks.map(check => ({ check, passed: true, detail: "Passed." })) };
  Object.assign(m.data.plugins[0], { active: GEN, enabled: true });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await expect(page.getByText("Agent enabled. Your saved defaults and budgets are unchanged.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run verification again" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Back to account setup" })).toHaveCount(0);
  expect(m.writes).toHaveLength(0);
});

for (const kind of ["review", "install", "signin", "verify", "activate"] as const) {
  test(`browser Back fences a delayed ${kind} response to its setup target`, async ({ page }) => {
    const m = await fixture(page, true);
    const other = { ...generation(), id: "older-candidate", review: structuredClone(REVIEW) };
    other.review.entry.manifest.identity.label = "Older installation";
    m.data.plugins[0].generations!.push(other);
    if (kind === "activate") m.gen.verification = { digest: REVIEW.digest, checked_at: 2,
      results: m.gen.required_checks.map(check => ({ check, passed: true, detail: "Passed" })) };
    const target = `${PATH}?plugin=sample-agent&generation=older-candidate`;
    const current = `${PATH}?plugin=sample-agent${["review", "install"].includes(kind) ? "" : `&generation=${GEN}`}`;
    await page.addInitScript(({ target, current }) => {
      history.replaceState({ ...history.state, idx: 0 }, "", target);
      history.pushState({ ...history.state, idx: 1 }, "", current);
    }, { target, current });
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    let requested = false;
    await page.route(`**/api/plugins/${kind}`, async route => {
      requested = true;
      const body = route.request().postDataJSON();
      await held;
      await route.fulfill({ json: kind === "review" ? REVIEW : {
        id: body.request_id, plugin_id: "sample-agent", generation_id: GEN,
        kind, state: kind === "install" ? "installed" : kind === "signin" ? "ready" : kind === "verify" ? "verified" : "complete",
        review: REVIEW, created_at: 1, updated_at: 1, review_digest: REVIEW.digest, error: null,
      } });
    });
    await page.goto(current);
    if (kind === "install") await page.getByRole("button", { name: "Review installation" }).click();
    if (kind === "verify") await page.getByRole("button", { name: "Continue to verification" }).click();
    const action = { review: "Review installation", install: "Install", signin: "Start sign-in", verify: "Verify installation", activate: "Enable agent" }[kind];
    await page.getByRole("button", { name: action, exact: true }).click();
    await expect.poll(() => requested).toBe(true);
    await page.goBack();
    await expect(page).toHaveURL(new RegExp("generation=older-candidate$"));
    await expect(page.getByRole("heading", { name: "Older installation 1.2.3" })).toBeVisible();
    const response = page.waitForResponse(r => r.url().endsWith(`/api/plugins/${kind}`) && r.request().method() === "POST");
    release(); await response;
    await expect(page.getByRole("button", { name: "Continue to verification" })).toBeEnabled();
    await expect(page.getByRole("heading", { name: "Older installation 1.2.3" })).toBeVisible();
    await expect(page.getByText(/Agent enabled\. Your saved defaults/)).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Operation status" })).toHaveCount(0);
    expect(m.sockets).toEqual([]);
    expect(posts(m, "/reload")).toHaveLength(0);
  });
}

for (const field of ["Base URL", "API key"]) test(`candidate endpoint discards a test when ${field} changes`, async ({ page }) => {
  const m = await fixture(page, true);
  m.gen.review.entry.manifest.runtime = { kind: "chat" };
  await page.route("**/generations/*/endpoint", r => r.fulfill({ json: {
    base_url: "https://one.test/v1", model: "sample", api_key_set: true, configured: true,
    context_window: 32768, max_output_tokens: 4096, request_timeout: null, tools: "none",
  } }));
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let requested = false;
  await page.route("**/generations/*/endpoint/test", async r => {
    requested = true; await held;
    await r.fulfill({ json: { listing: "ok", models: ["old-endpoint-model"] } });
  });
  await page.goto(`${PATH}?plugin=sample-agent&generation=${GEN}`);
  await expect(page.getByLabel("Base URL")).toHaveValue("https://one.test/v1");
  await page.getByRole("button", { name: "Test", exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await page.getByLabel(field, { exact: true }).fill(field === "Base URL" ? "https://two.test/v1" : "new-test-key");
  release();
  await expect(page.getByRole("button", { name: "Test", exact: true })).toBeEnabled();
  await expect(page.getByTestId("endpoint-status")).toHaveCount(0);
  await expect(page.locator("datalist option")).toHaveCount(0);
  await page.getByRole("button", { name: "Test", exact: true }).click();
  await expect(page.getByTestId("endpoint-status")).toContainText("The endpoint answered");
  await page.getByLabel(field, { exact: true }).fill(field === "Base URL" ? "https://three.test/v1" : "newer-test-key");
  await expect(page.getByTestId("endpoint-status")).toHaveCount(0);
  await expect(page.locator("datalist option")).toHaveCount(0);
});
