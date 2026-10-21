import { expect, test, type Page } from "@playwright/test";
import { PLAYBOOKS_PATH } from "../src/lib/routes";
import { commonMocks } from "./mission-directions";

const REV = "a".repeat(64);
const NEW_REV = "b".repeat(64);
const card = (id = "team-workflow", source = "local") => ({
  id,
  name: "Team workflow",
  source,
  editable: source === "local",
  ok: true,
  error: null,
  revision: REV,
  default: false,
  version: "1.0.0",
  domain: "development",
  publisher: "Example",
  summary: "Implement and independently review a change.",
  default_flow: "dev",
  flows: [
    {
      id: "dev",
      title: "Ship a change",
      steps: [
        {
          id: "implement",
          title: "Implement",
          actor: {
            kind: "agent",
            engine: "unavailable-agent",
            model: "careful",
          },
          after: [],
          note: false,
        },
        {
          id: "review",
          title: "Independent review",
          actor: { kind: "external", label: "Reviewer" },
          after: ["implement"],
          note: false,
        },
      ],
    },
  ],
  ships: { materials: 1, runbooks: 0, templates: 1, variables: 1 },
  requires: { binaries: ["git"], connections: [] },
  connections: [],
  capabilities: [],
});

async function setup(
  page: Page,
  opts: {
    stale?: boolean;
    inUse?: boolean;
    fail?: boolean;
    lostUpdate?: boolean;
  } = {},
) {
  await commonMocks(page);
  const calls: { method: string; path: string; body: unknown }[] = [];
  let rows = [
    card(),
    { ...card("starter", "bundled"), name: "Starter", domain: "operations" },
    {
      id: "broken",
      source: "local",
      editable: true,
      ok: false,
      error: "Unsupported bundle format",
      revision: REV,
      default: false,
    },
  ];
  let currentDefault: string | null = null;
  let stale = opts.stale;
  let lostUpdate = opts.lostUpdate;
  await page.route("**/api/playbooks**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const parts = path.split("/");
    const id = parts[3];
    const method = req.method();
    const body = req.postData() ? req.postDataJSON() : undefined;
    if (method !== "GET") calls.push({ method, path, body });
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ status, json });
    if (!id)
      return opts.fail
        ? reply({ detail: "Store unavailable" }, 503)
        : reply({
            playbooks: rows,
            default: currentDefault,
            recovery: [],
            recovery_total: 0,
          });
    if (path.endsWith("/projects"))
      return reply({
        playbook_id: id,
        revision: REV,
        projects: [
          {
            project_id: "project-a",
            deployment_id: "deploy-a",
            state: "applied",
            revision: "c".repeat(64),
            update_available: true,
          },
          {
            project_id: "project-b",
            deployment_id: "deploy-b",
            state: "bound",
            revision: "d".repeat(64),
            update_available: true,
          },
        ],
      });
    if (path.endsWith("/fleet/review"))
      return reply({
        playbook_id: id,
        revision: REV,
        digest: NEW_REV,
        projects: [
          {
            project_id: "project-a",
            batchable: true,
            reasons: [],
            changes: [
              {
                path: "RULES.md",
                action: "update",
                diff: "-old instruction\n+new instruction",
              },
            ],
          },
          {
            project_id: "project-b",
            batchable: false,
            reasons: ["the deployment is bound"],
            changes: [],
          },
        ],
      });
    if (path.endsWith("/fleet/update")) {
      if (lostUpdate) {
        lostUpdate = false;
        return route.abort("failed");
      }
      return reply({
        playbook_id: id,
        operation_id: body.operation_id,
        revision: REV,
        projects: [{ project_id: "project-a", outcome: "applied", detail: "" }],
      });
    }
    if (path.includes("/fleet/") && method === "GET")
      return reply({
        operation_id: parts[5],
        projects: [
          {
            project_id: "project-a",
            outcome: "not-attempted",
            detail: "Interrupted before the first project",
          },
        ],
      });
    if (path.endsWith("/duplicate")) {
      const copy = {
        ...card("team-copy"),
        name: "Team workflow (copy)",
        flows: [{ id: "dev-copy", title: "Ship a change", steps: [] }],
      };
      rows = [...rows, copy];
      return reply(
        {
          ...copy,
          files: {},
          documents: {},
          readme: "",
          requires_present: { binaries: {} },
        },
        201,
      );
    }
    if (path.endsWith("/default")) {
      if (stale) {
        stale = false;
        rows = rows.map((r) => (r.id === id ? { ...r, revision: NEW_REV } : r));
        return reply(
          {
            detail: "The playbook changed elsewhere",
            current: { ...card(), revision: NEW_REV },
          },
          409,
        );
      }
      currentDefault = method === "DELETE" ? null : id;
      rows = rows.map((r) => ({ ...r, default: r.id === currentDefault }));
      return reply({ default: currentDefault });
    }
    if (method === "DELETE") {
      if (opts.inUse)
        return reply(
          {
            detail: "Projects still run this playbook",
            projects: [{ id: "project-a", name: "Upload service" }],
          },
          409,
        );
      rows = rows.filter((r) => r.id !== id);
      if (currentDefault === id) currentDefault = null;
      return reply({ deleted: id, default: currentDefault });
    }
    const found = rows.find((r) => r.id === id);
    return found
      ? reply({
          ...found,
          readme: "# Instructions\nKeep the release branch untouched.",
          files: { "template/RULES.md": "Keep the release branch untouched." },
          documents: {
            "playbook.toml": {
              format: 2,
              variables: [{ name: "repo", type: "text", required: true }],
              materials: [{ path: "RULES.md", disposition: "managed" }],
            },
          },
          requires_present: { binaries: { git: true } },
          recovery: [],
          recovery_total: 0,
        })
      : reply({ detail: "Not found" }, 404);
  });
  await page.route("**/api/projects/*/playbook/verify", (r) =>
    r.fulfill({
      json: {
        project_id: "project-a",
        deployment_id: "deploy-a",
        ok: false,
        checks: { materials: { ok: false, drift: ["RULES.md"] } },
      },
    }),
  );
  return calls;
}

test("gallery filters, independent invalid cards and the detail route", async ({
  page,
}) => {
  await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  await expect(page.getByTestId("playbook-card-team-workflow")).toBeVisible();
  await expect(page.getByTestId("playbook-card-team-workflow")).toContainText(
    "Unresolved: agent absent",
  );
  await expect(page.getByTestId("playbook-card-team-workflow")).toContainText(
    "careful",
  );
  await expect(page.getByTestId("playbook-card-broken")).toContainText(
    "Unsupported bundle format",
  );
  await expect(
    page.getByTestId("playbook-card-broken").getByRole("link"),
  ).toHaveCount(0);
  await page.getByLabel("Search playbooks").fill("independent");
  await expect(page.getByTestId("playbook-card-starter")).toBeVisible();
  await page.getByLabel("Source").selectOption("local");
  await expect(page.getByTestId("playbook-card-starter")).toHaveCount(0);
  await page
    .getByTestId("playbook-card-team-workflow")
    .getByRole("link", { name: "Open" })
    .click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Team workflow" }),
  ).toBeVisible();
  await expect(
    page.getByText("Keep the release branch untouched.").first(),
  ).toBeVisible();
  await expect(
    page.locator('.hud-topbar [data-section="library"]'),
  ).toHaveAttribute("aria-current", "true");
  await expect(page.locator(".app")).toHaveClass(/noSidebar/);
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: "Team workflow" }),
  ).toBeVisible();
});

test("default conflicts require a refresh and a fresh explicit action", async ({
  page,
}) => {
  const calls = await setup(page, { stale: true });
  await page.goto(`${PLAYBOOKS_PATH}/team-workflow`);
  await page
    .getByRole("button", { name: "Set as default", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("changed elsewhere");
  expect(calls).toHaveLength(1);
  await page.getByRole("button", { name: "Reload current playbooks" }).click();
  await page
    .getByRole("button", { name: "Set as default", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Clear default", exact: true }),
  ).toBeVisible();
  expect(calls[0].body).toEqual({ revision: REV, expect_default: null });
  expect(calls[1].body).toEqual({ revision: NEW_REV, expect_default: null });
});

test("duplicate opens the returned local playbook; delete refuses with named projects", async ({
  page,
}) => {
  const calls = await setup(page, { inUse: true });
  await page.goto(`${PLAYBOOKS_PATH}/starter`);
  await expect(
    page.getByRole("button", { name: "Delete playbook…" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Duplicate to local", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}/team-copy$`));
  expect(calls[0].body).toEqual({ revision: REV });
  await page.getByRole("button", { name: "Delete playbook…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await dialog
    .getByRole("button", { name: "Delete playbook", exact: true })
    .click();
  await expect(dialog).toContainText("Upload service");
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}/team-copy$`));
});

test("fleet shows server reasons, verifies on demand and approves exact reviewed changes", async ({
  page,
}) => {
  const calls = await setup(page);
  await page.goto(`${PLAYBOOKS_PATH}/team-workflow`);
  await expect(page.getByTestId("fleet-project-a")).toContainText(
    "Not verified",
  );
  await page
    .getByTestId("fleet-project-a")
    .getByRole("button", { name: "Verify" })
    .click();
  await expect(page.getByTestId("fleet-project-a")).toContainText("RULES.md");
  await page.getByRole("button", { name: "Update projects…" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "the deployment is bound",
  );
  await page
    .getByRole("button", { name: "Show changes for project-a" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("+new instruction");
  await page
    .getByRole("button", { name: "Apply to 1 project", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("project-a: applied");
  expect(calls.at(-1)?.body).toMatchObject({ digest: NEW_REV });
  expect(
    (calls.at(-1)?.body as { operation_id: string }).operation_id,
  ).toBeTruthy();
});

test("320px gallery and detail hold touch targets without horizontal overflow", async ({
  page,
}) => {
  await setup(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(PLAYBOOKS_PATH);
  await expect(page.getByTestId("playbook-card-team-workflow")).toBeVisible();
  for (const path of [PLAYBOOKS_PATH, `${PLAYBOOKS_PATH}/team-workflow`]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    for (const el of await page
      .getByTestId("playbooks-page")
      .locator("button:visible, a:visible, select:visible, input:visible")
      .all()) {
      expect((await el.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
  }
});

test("failed gallery is an error, not an empty collection", async ({
  page,
}) => {
  await setup(page, { fail: true });
  await page.goto(PLAYBOOKS_PATH);
  await expect(page.getByRole("alert")).toContainText("Store unavailable");
  await expect(page.getByText("No playbooks on this host.")).toHaveCount(0);
});

test("a lost update keeps its identity through cancel and reload", async ({
  page,
}) => {
  const calls = await setup(page, { lostUpdate: true });
  await page.goto(`${PLAYBOOKS_PATH}/team-workflow`);
  await page.getByRole("button", { name: "Update projects…" }).click();
  await page
    .getByRole("button", { name: "Apply to 1 project", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Retry uses the same reviewed update",
  );
  const original = calls.find((c) => c.path.endsWith("/fleet/update"))!.body;
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "Resume reviewed update…" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Interrupted before the first project",
  );
  await page
    .getByRole("button", { name: "Retry reviewed update", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("project-a: applied");
  expect(
    calls.filter((c) => c.path.endsWith("/fleet/update")).map((c) => c.body),
  ).toEqual([original, original]);
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("battlelab.playbook-fleet.team-workflow"),
    ),
  ).toBeNull();
});

test("fleet refuses submission when the retry identity cannot be saved", async ({
  page,
}) => {
  const calls = await setup(page);
  await page.goto(`${PLAYBOOKS_PATH}/team-workflow`);
  await page.getByRole("button", { name: "Update projects…" }).click();
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("Storage denied");
    };
  });
  await page
    .getByRole("button", { name: "Apply to 1 project", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Browser storage is unavailable",
  );
  expect(calls.filter((c) => c.path.endsWith("/fleet/update"))).toHaveLength(0);
});
