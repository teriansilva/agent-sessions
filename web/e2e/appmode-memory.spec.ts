/** Shared project memory (#1441) end to end on the real Python stack: a project's memory is turned
 *  on through the real API, a note and a credential-shaped file land in a launch inbox exactly as
 *  an agent writes them (the binding of that inbox to a launch is pinned by the route-level
 *  pytest), and the real page takes them in on open — the note in the list and the graph, the
 *  credential masked in quarantine — then edit, delete and clear go through the real store. */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, utimesSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";
import { HOOK_TIMEOUT_MS } from "./harness";

test.describe.configure({ mode: "serial" });
test.setTimeout(90_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

let stack: Stack;
let pid = "";
let inbox = "";

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stack = await startStack();
  const prefs = join(stack.home, ".config", "agent-sessions");
  mkdirSync(prefs, { recursive: true });
  writeFileSync(join(prefs, "prefs.json"), JSON.stringify({ onboarded: true, whats_new_seen: "0.20.0" }));
});
test.afterAll(async () => { await stack?.stop(); });

const base = () => `http://127.0.0.1:${stack.appPort}`;

async function write(page: Page, method: "POST" | "PUT", path: string, data: unknown) {
  const csrf = (await (await page.request.get(`${base()}/api/config`)).json()).csrf as string;
  const r = await page.request.fetch(`${base()}${path}`, {
    method,
    data,
    headers: { "X-CSRF-Token": csrf, Origin: base() },
  });
  expect(r.status(), `${method} ${path}: ${await r.text()}`).toBeLessThan(300);
  return r.json();
}

/** What an agent does with `$BATTLELAB_MEMORY_INBOX`: write one markdown file. Aged past the
 *  settle window so the next pass takes it. */
function drop(name: string, text: string) {
  const path = join(inbox, name);
  writeFileSync(path, text);
  const old = new Date(Date.now() - 60_000);
  utimesSync(path, old, old);
}

test("memory is off until turned on; a dropped note and a withheld credential appear on open", async ({ page }) => {
  await page.goto(`${base()}/library/memory`);
  const project = await write(page, "POST", "/api/projects", { name: "memory-e2e", folders: [stack.repo] });
  pid = project.id;
  await page.goto(`${base()}/library/memory?project=${pid}`);
  await expect(page.getByRole("button", { name: "Turn memory on" })).toBeVisible();
  await page.getByRole("button", { name: "Turn memory on" }).click();
  await expect(page.getByRole("switch", { name: "Shared memory for memory-e2e" })).toHaveAttribute("aria-checked", "true");

  inbox = join(stack.home, ".config", "agent-sessions", "memory", pid, "inbox", `m${randomBytes(16).toString("hex")}`);
  mkdirSync(inbox, { recursive: true, mode: 0o700 });
  drop("typed-ids.md", "---\nname: Scopes are typed ids\ntype: decision\n---\n\nUse the project id, never its name.\n");
  drop("deploy.md", `# Deploy\n\ntoken=ghp_${"a".repeat(36)}\nthen push.\n`);

  await page.reload(); // opening the page runs an ingest pass
  if (page.viewportSize()!.width <= 800) {
    await expect(page.getByRole("button", { name: /Scopes are typed ids/ })).toBeVisible();
    await page.getByRole("tab", { name: /Trays/ }).click();
  } else {
    await expect(page.getByTestId("memory-node-scopes-are-typed-ids")).toBeVisible();
  }
  const quarantined = page.getByLabel("Quarantined deploy.md");
  await expect(quarantined).toContainText("████████ (withheld)");
  await expect(page.locator("body")).not.toContainText("ghp_");
  const view = await (await page.request.get(`${base()}/api/memory/${pid}`)).json();
  expect(view.notes.map((n: { slug: string }) => n.slug)).toEqual(["scopes-are-typed-ids"]);
});

test("edit, delete and clear go through the real store", async ({ page }) => {
  await page.goto(`${base()}/library/memory?project=${pid}`);
  if (page.viewportSize()!.width <= 800) await page.getByRole("button", { name: /Scopes are typed ids/ }).click();
  else await page.getByTestId("memory-node-scopes-are-typed-ids").click();
  const inspector = page.getByRole("complementary", { name: "Note: Scopes are typed ids" });
  await inspector.getByRole("button", { name: "Edit" }).click();
  await inspector.getByLabel("Body").fill("Use the project entity id.");
  await inspector.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toContainText("Saved");
  let view = await (await page.request.get(`${base()}/api/memory/${pid}`)).json();
  expect(view.notes[0].body).toBe("Use the project entity id.");

  await page.getByRole("complementary", { name: /Note: Scopes/ }).getByRole("button", { name: "Delete" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("status")).toContainText("Deleted");
  view = await (await page.request.get(`${base()}/api/memory/${pid}`)).json();
  expect(view.notes).toEqual([]);

  drop("again.md", "# Another note\n\nbody\n");
  await page.reload();
  await page.getByRole("button", { name: "Clear…" }).click();
  await page.getByRole("dialog").getByRole("textbox").fill("memory-e2e");
  await page.getByRole("dialog").getByRole("button", { name: "Clear memory" }).click();
  await expect(page.getByRole("status")).toContainText("Memory cleared");
  view = await (await page.request.get(`${base()}/api/memory/${pid}`)).json();
  expect(view.notes).toEqual([]);
  expect(view.quarantine).toEqual([]);
});
