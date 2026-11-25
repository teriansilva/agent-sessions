import { expect, test, type Page } from "@playwright/test";
import { projectPath } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// Real-browser check of HQ → Projects → a project's Playbook tab (#2143 P2, #1949): deploy a playbook
// onto an EXISTING project through the reviewed flow (review → confirm → bind → apply → verify),
// verify again, remove it through its reviewed plan; Retry resends the recorded operation; an
// archived project gets no deploy action; a refused apply shows the server's words; four tabs fit a
// phone. A small stateful fake of the deployment API stands in for the server, on the desktop AND
// mobile projects. The same journey against the real server is appmode-project-playbook-tab.spec.ts.

const PID = "p-1";
const ENTITY = {
  id: PID,
  name: "SampleProject",
  color: "#5fd7ff",
  folders: ["/home/u/sample"],
  default_folder: "/home/u/sample",
  archived: false,
  created_at: 0,
  session_count: 2,
};
const REV = "a".repeat(64);
const CARD = {
  id: "ship-it",
  name: "Ship it",
  ok: true,
  error: null,
  source: "bundled",
  editable: false,
  revision: REV,
  default: false,
  version: "1.2.0",
  summary: "Plan, build, review and land a change.",
  flows: [{ id: "main", title: "Main", steps: [{ id: "plan", title: "Plan", actor: { kind: "operator" } }] }],
};
const DETAIL = {
  ...CARD,
  recovery_total: 0,
  files: {},
  documents: { "playbook.toml": { format: 2, variables: [{ name: "DEPLOY_HOST", type: "text", default: "box" }] } },
};
const REVIEW = {
  digest: "b".repeat(64),
  deployment_id: "d-1",
  destination: { path: "/home/u/sample", device: 1, inode: 2 },
  variables: [{ name: "DEPLOY_HOST", kind: "text", type: "text", state: "ok", source: "default", revision: null, value: "box" }],
  materials: [],
  changes: [{ path: "RULES.md", action: "create", diff: "+Read the rules." }],
  conflicts: [],
  targets: [],
  assignments: [],
  capability_requests: [],
  rituals: [],
};
const VERIFIED = { deployment_id: "d-1", ok: true, update_available: false, checks: { materials: { ok: true, drift: [] }, variables: { ok: true, problems: {} } } };

type Fake = {
  state: "none" | "bound" | "applied" | "removed" | "interrupted";
  calls: { path: string; body: Record<string, unknown> | null }[];
  verifies: number;
  /** Refuse the next apply: a 503 leaves an interrupted journal under its id; a 409 refuses it. */
  applyFails: null | 503 | 409;
  op: string | null;
  /** Which operation `op` names while the state is interrupted (#2263). */
  kind: "bind" | "apply" | "remove";
};
/** A request-free body: exactly `{operation_id}`, the resume by id (#2263). */
const resumeBody = (body: Record<string, unknown> | null) =>
  !!body && Object.keys(body).length === 1 && typeof body.operation_id === "string";

async function mock(page: Page, entity = ENTITY, start: Fake["state"] = "none") {
  const fake: Fake = { state: start, calls: [], verifies: 0, applyFails: null, op: null, kind: "apply" };
  const json = (r: Parameters<Parameters<Page["route"]>[1]>[0], body: unknown, status = 200) =>
    r.fulfill({ status, json: body });
  await page.route("**/api/config", (r) =>
    json(r, { csrf: "x", new_session_engines: ["claude"], terminal_backend: "ws", auth_mode: "none", overview_expanded: [], projects_hidden: [], onboarded: true }),
  );
  await page.route("**/api/version", (r) => json(r, { version: "test" }));
  await page.route("**/api/engines", (r) => json(r, { engines: [] }));
  await page.route("**/api/system", (r) => json(r, {}));
  await page.route("**/api/pulse/notifications", (r) => json(r, { notifications: [], unread: 0, uncertain: 0, settled: [] }));
  await page.route("**/api/sessions**", (r) => json(r, { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } }));
  await page.route("**/api/template-variables", (r) => json(r, { variables: [] }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => json(r, { projects: [entity] }));
  await page.route("**/api/playbooks**", async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    fake.calls.push({ path, body: req.method() === "POST" ? req.postDataJSON() : null });
    if (path === "/api/playbooks") return json(r, { playbooks: [CARD], default: null, recovery_total: 0 });
    if (path === "/api/playbooks/authoring/schema") return json(r, { agents: [] });
    if (path === "/api/playbooks/ship-it") return json(r, DETAIL);
    if (path === "/api/playbooks/ship-it/projects")
      return json(r, {
        playbook_id: "ship-it",
        revision: REV,
        projects: fake.state === "applied" ? [{ project_id: PID, deployment_id: "d-1", state: "applied", revision: REV, update_available: false }] : [],
      });
    if (path === "/api/playbooks/ship-it/review") return json(r, REVIEW);
    if (path === "/api/playbooks/ship-it/review/confirm") return json(r, { receipt: "signed-review", digest: REVIEW.digest });
    return json(r, { detail: "not mocked" }, 404);
  });
  await page.route(`**/api/projects/${PID}/playbook**`, async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    const body = req.method() === "POST" ? (req.postDataJSON() as Record<string, unknown>) : null;
    fake.calls.push({ path, body });
    const tail = path.slice(`/api/projects/${PID}/playbook`.length);
    const status = () =>
      fake.state === "none"
        ? { project_id: PID, state: "none" }
        : {
            project_id: PID,
            deployment_id: "d-1",
            playbook_id: "ship-it",
            state: fake.state,
            ...(fake.state === "interrupted"
              ? {
                  operation: fake.kind,
                  operation_id: fake.op,
                  files:
                    fake.kind === "bind"
                      ? undefined
                      : [{ path: "RULES.md", state: fake.kind === "remove" ? "not removed" : "not written" }],
                }
              : {}),
          };
    if (tail === "") return json(r, status());
    if (tail === "/verify") {
      fake.verifies++;
      return fake.state === "applied" ? json(r, VERIFIED) : json(r, { detail: "there is no applied deployment to verify" }, 409);
    }
    // A resume settles only the interrupted operation its id names; anything else is refused.
    if (resumeBody(body) && (tail === "/bind" || tail === "/apply" || tail === "/remove")) {
      if (fake.state !== "interrupted" || fake.op !== body!.operation_id || `/${fake.kind}` !== tail)
        return json(r, { detail: "no operation is pending or recorded under this id" }, 409);
      if (fake.kind === "bind") {
        fake.state = "bound";
        return json(r, { receipt: "resumed-receipt", digest: "c".repeat(64) });
      }
      fake.state = fake.kind === "apply" ? "applied" : "removed";
      return json(r, { project_id: PID, deployment_id: "d-1", state: fake.state });
    }
    if (tail === "/bind") {
      fake.state = "bound";
      return json(r, { receipt: "bound-receipt", digest: "c".repeat(64) });
    }
    if (tail === "/apply") {
      if (fake.applyFails === 409) {
        fake.applyFails = null;
        return json(r, { detail: "the destination project is missing or archived" }, 409);
      }
      if (fake.applyFails === 503) {
        fake.applyFails = null;
        fake.state = "interrupted";
        fake.op = String(body?.operation_id);
        return json(r, { detail: "Service unavailable" }, 503);
      }
      if (fake.state === "interrupted" && body?.operation_id !== fake.op)
        return json(r, { detail: "retry the pending apply operation before another" }, 409);
      fake.state = "applied";
      return json(r, { project_id: PID, deployment_id: "d-1", state: "applied" });
    }
    if (tail === "/remove/plan")
      return json(r, {
        digest: "e".repeat(64),
        changes: [{ path: "RULES.md", action: "remove" }],
        conflicts: [],
        bindings: { remove: ["DEPLOY_HOST"], restore: [], refusal: null },
        directories: [],
        secret_files: [],
      });
    if (tail === "/remove") {
      fake.state = "removed";
      return json(r, { project_id: PID, state: "removed" });
    }
    return json(r, { detail: "not mocked" }, 404);
  });
  return fake;
}

const writes = (fake: Fake) =>
  fake.calls.filter((c) => c.body !== null).map((c) => c.path.replace(`/api/projects/${PID}/playbook`, "…").replace("/api/playbooks/ship-it", "…"));

async function chooseAndReview(page: Page) {
  await page.getByRole("radiogroup", { name: "Playbook" }).getByRole("radio", { name: /Ship it/ }).check();
  await page.getByRole("button", { name: "Review playbook changes" }).click();
  await expect(page.getByText("create · RULES.md")).toBeVisible();
}

test("deploy onto an existing project → verify → verify again → remove, each through its review", async ({ page }) => {
  const fake = await mock(page);
  await page.goto(projectPath(PID, "playbook"));
  await expect(page.getByRole("tab", { name: "Playbook" })).toHaveAttribute("aria-selected", "true");
  await chooseAndReview(page);
  // Review is read-only: nothing is bound or applied until the operator applies it.
  expect(writes(fake)).toEqual(["…/review"]);
  await page.getByRole("button", { name: "Apply reviewed changes to this project" }).click();
  const verify = page.getByTestId("playbook-verify");
  await expect(verify).toContainText("Verified");
  await expect(page.getByTestId("playbook-done")).toHaveText("Applied and verified.");
  expect(writes(fake)).toEqual(["…/review", "…/review/confirm", "…/bind", "…/apply"]);
  // The existing project, never a folder to create.
  const reviewed = fake.calls.find((c) => c.path.endsWith("/review"))!.body!;
  expect(reviewed).toMatchObject({ project_id: PID, destination: "/home/u/sample" });
  expect(reviewed).not.toHaveProperty("create");
  // The deploy's own verify is shown; the tab doesn't run a second one.
  expect(fake.verifies).toBe(1);
  await page.getByRole("button", { name: "Verify again" }).click();
  await expect.poll(() => fake.verifies).toBe(2);

  await page.getByTestId("playbook-remove").click();
  const dialog = page.getByRole("dialog", { name: "Review removal" });
  await expect(dialog).toContainText("RULES.md");
  await dialog.getByRole("button", { name: "Remove reviewed playbook" }).click();
  await expect(page.getByTestId("playbook-done")).toHaveText("Removed. The project and its folders stay.");
  await expect(page.getByRole("radiogroup", { name: "Playbook" })).toBeVisible();
  expect(writes(fake).slice(-2)).toEqual(["…/remove/plan", "…/remove"]);
});

test("an interrupted apply resumes after a reload by its id alone, and never starts another", async ({ page }) => {
  const fake = await mock(page);
  fake.applyFails = 503;
  await page.goto(projectPath(PID, "playbook"));
  await chooseAndReview(page);
  await page.getByRole("button", { name: "Apply reviewed changes to this project" }).click();
  await expect(page.getByTestId("playbook-interrupted")).toBeVisible();
  expect(fake.op).toMatch(/\S/);
  // A fresh page: nothing of the original request survives the reload.
  await page.reload();
  const box = page.getByTestId("playbook-interrupted");
  await expect(box).toContainText(fake.op!);
  await page.getByTestId("playbook-resume").click();
  await expect(page.getByTestId("playbook-verify")).toContainText("Verified");
  const applies = fake.calls.filter((c) => c.path.endsWith("/apply")).map((c) => c.body);
  expect(applies.at(-1)).toEqual({ operation_id: fake.op });
  expect(applies).toHaveLength(2);
  expect(fake.calls.filter((c) => c.path.endsWith("/bind"))).toHaveLength(1);
});

test("an interrupted bind resumes on a page that never held its inputs, stops at bound, then applies as its own step", async ({ page }, info) => {
  const fake = await mock(page, ENTITY, "interrupted");
  fake.kind = "bind";
  fake.op = "bind-op-recorded";
  await page.goto(projectPath(PID, "playbook"));
  const resume = page.getByTestId("playbook-resume");
  await expect(resume).toHaveText(/Resume binding/);
  if (info.project.name === "mobile") {
    const box = (await resume.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await resume.click();
  await expect(page.getByTestId("playbook-done")).toHaveText("Binding finished. Nothing is written until you apply.");
  expect(fake.calls.filter((c) => c.path.endsWith("/bind")).map((c) => c.body)).toEqual([{ operation_id: "bind-op-recorded" }]);
  // It stopped at bound: no apply until the operator asks for one.
  expect(fake.calls.filter((c) => c.path.endsWith("/apply"))).toHaveLength(0);
  await page.getByTestId("playbook-apply-bound").click();
  await expect(page.getByTestId("playbook-verify")).toContainText("Verified");
  const apply = fake.calls.find((c) => c.path.endsWith("/apply"))!.body!;
  expect(apply.receipt).toBe("resumed-receipt");
  expect(apply.operation_id).not.toBe("bind-op-recorded");
});

test("an interrupted removal resumes on a fresh page by its id alone", async ({ page }) => {
  const fake = await mock(page, ENTITY, "interrupted");
  fake.kind = "remove";
  fake.op = "remove-op-recorded";
  await page.goto(projectPath(PID, "playbook"));
  await expect(page.getByTestId("playbook-interrupted")).toContainText("not removed");
  await page.getByTestId("playbook-resume").click();
  await expect(page.getByTestId("playbook-done")).toHaveText("Removed. The project and its folders stay.");
  await expect(page.getByRole("radiogroup", { name: "Playbook" })).toBeVisible();
  expect(writes(fake)).toEqual(["…/remove"]);
  expect(fake.calls.filter((c) => c.body).map((c) => c.body)).toEqual([{ operation_id: "remove-op-recorded" }]);
});

test("a refused apply shows the server's words; an archived project gets no deploy action", async ({ page }) => {
  const fake = await mock(page);
  fake.applyFails = 409;
  await page.goto(projectPath(PID, "playbook"));
  await chooseAndReview(page);
  await page.getByRole("button", { name: "Apply reviewed changes to this project" }).click();
  await expect(page.getByTestId("playbook-error")).toHaveText("the destination project is missing or archived");

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mock(page, { ...ENTITY, archived: true });
  await page.goto(projectPath(PID, "playbook"));
  await expect(page.getByTestId("playbook-archived")).toBeVisible();
  await expect(page.getByRole("radiogroup", { name: "Playbook" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Apply reviewed/ })).toHaveCount(0);
});

test("on a phone the four project tabs fit the screen at the touch floor", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "phone layout");
  await mock(page, ENTITY, "applied");
  await page.goto(projectPath(PID, "playbook"));
  await expect(page.getByTestId("playbook-verify")).toBeVisible();
  const width = page.viewportSize()!.width;
  for (const name of ["General", "Session defaults", "Playbook", "Memory & equipment"]) {
    const box = (await page.getByRole("tab", { name }).boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  for (const name of ["Verify again", "Change playbook…", "Remove playbook…"]) {
    const box = (await page.getByRole("button", { name }).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
});
