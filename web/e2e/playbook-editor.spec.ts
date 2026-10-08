import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { PLAYBOOKS_PATH, playbookEditPath } from "../src/lib/routes";
import { readFileSync } from "node:fs";
const roster = JSON.parse(
  readFileSync(
    new URL("../src/test/roster.fixture.json", import.meta.url),
    "utf8",
  ),
);
import type {
  AuthoringSchema,
  Files,
  Flow,
  Manifest,
} from "../src/components/playbooks/playbookDraft";

const REV = "a".repeat(64),
  NEXT = "b".repeat(64);
const schema: AuthoringSchema = {
  agents: ["claude", "codex"],
  actors: ["agent", "external", "none", "operator"],
  memory: ["none", "read", "read-write"],
  distinct: ["session", "engine", "model"],
  variable_types: ["text", "url", "enum", "bool", "int", "path"],
  probes: {
    supervisor_judged: { outputs: [], args: {} },
    none: { outputs: [], args: {} },
    forge_pr: {
      outputs: ["pr_number", "head_branch", "head_sha"],
      args: {
        repo: {
          required: false,
          type: "text",
          literal: false,
          variable_types: ["text", "enum"],
          slots: [],
        },
      },
    },
    forge_review: {
      outputs: [],
      args: {
        branch: {
          required: false,
          type: "text",
          literal: false,
          variable_types: ["text", "enum"],
          slots: ["head_branch", "branch"],
        },
      },
    },
  },
  limits: {
    flows: 20,
    steps: 30,
    items: 12,
    variables: 32,
    distinct: 8,
    rework_min: 1,
    rework_max: 10,
  },
};
async function setup(
  page: Page,
  options: {
    stale?: boolean;
    invalid?: boolean;
    readonly?: boolean;
    collision?: boolean;
    flow?: Flow;
  } = {},
) {
  await commonMocks(page);
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: roster.engines } }),
  );
  let manifest: Manifest = {
    format: 2,
    identity: {
      id: "team-flow",
      name: "Team flow",
      publisher: "Team",
      version: "1.0.0",
      domain: "development",
    },
    flows: { default: "main" },
  };
  let flow: Flow = options.flow ?? {
    format: 2,
    title: "Main flow",
    steps: [
      {
        id: "build",
        title: "Build",
        actor: {
          kind: "agent",
          engine: "missing-agent",
          model: "missing-model",
        },
        checklist: [
          { key: "built", title: "Built", probe: "supervisor_judged" },
        ],
      },
    ],
  };
  let documents: Record<string, unknown> = {
    "playbook.toml": manifest,
    "flows/main.toml": flow,
  };
  let files: Files = {
    "playbook.toml": "# original manifest",
    "flows/main.toml": "# original flow",
    "README.md": "Original README",
    "template/asset.bin": { base64: "AP+A/w==" },
  };
  let revision = REV;
  const calls: { method: string; body: { files: Files; revision?: string } }[] =
    [];
  const detail = () => ({
    id: manifest.identity.id,
    name: manifest.identity.name,
    source: options.readonly ? "bundled" : "local",
    editable: !options.readonly,
    ok: true,
    revision,
    recovery_total: 0,
    files,
    documents,
    readme: files["README.md"] ?? "",
    flows: [{ id: "main", ...flow }],
    requires: { binaries: [] },
  });
  await page.route("**/api/playbooks**", (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/authoring/schema"))
      return route.fulfill({ json: schema });
    if (req.method() === "GET" && path.endsWith("/projects"))
      return route.fulfill({
        json: { playbook_id: manifest.identity.id, revision, projects: [] },
      });
    if (req.method() === "GET")
      return route.fulfill({
        json:
          path === "/api/playbooks"
            ? { playbooks: [detail()], default: null, recovery_total: 0 }
            : detail(),
      });
    const body = req.postDataJSON();
    calls.push({ method: req.method(), body });
    if (
      options.collision &&
      path === "/api/playbooks" &&
      req.method() === "POST"
    ) {
      options.collision = false;
      return route.fulfill({
        status: 409,
        json: { detail: "A playbook with that ID already exists" },
      });
    }
    if (options.invalid) {
      options.invalid = false;
      return route.fulfill({
        status: 422,
        json: {
          detail: "flows/main.toml: steps[0].title is too long",
          field: "steps[0].title",
        },
      });
    }
    if (options.stale) {
      options.stale = false;
      revision = NEXT;
      manifest = {
        ...manifest,
        identity: { ...manifest.identity, name: "Changed elsewhere" },
      };
      documents = { ...documents, "playbook.toml": manifest };
      files = {
        ...files,
        "playbook.toml": "# New operator comment\nformat = 2\n",
        "template/asset.bin": { base64: "AP+A/g==" },
      };
      return route.fulfill({
        status: 409,
        json: { detail: "The playbook changed since you loaded it", revision },
      });
    }
    files = body.files;
    for (const [path, raw] of Object.entries(files))
      if (typeof raw === "object" && "toml" in raw) documents[path] = raw.toml;
    manifest = documents["playbook.toml"] as Manifest;
    flow = documents["flows/main.toml"] as Flow;
    if (path.endsWith("/authoring/copy")) {
      manifest = {
        ...manifest,
        identity: { ...manifest.identity, id: "draft-copy" },
      };
      documents["playbook.toml"] = manifest;
    }
    revision = NEXT;
    return route.fulfill({
      status: req.method() === "POST" ? 201 : 200,
      json: detail(),
    });
  });
  return { calls };
}

test("create, reorder, add rework, save, reload and edit two different agents", async ({
  page,
}) => {
  const { calls } = await setup(page);
  await page.goto(PLAYBOOKS_PATH);
  await page.getByRole("link", { name: "New playbook", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "New playbook", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Playbook name", { exact: true })
    .fill("Release process");
  await page.getByLabel("Playbook ID", { exact: true }).fill("release-process");
  await page.getByLabel("Step title", { exact: true }).fill("Implement");
  await page.getByLabel("Actor", { exact: true }).selectOption("agent");
  await page.getByLabel("Agent", { exact: true }).selectOption("claude");
  await page.getByRole("button", { name: "Add step", exact: true }).click();
  await page.getByLabel("Step title", { exact: true }).fill("Review");
  await page.getByLabel("Actor", { exact: true }).selectOption("agent");
  await page.getByLabel("Agent", { exact: true }).selectOption("codex");
  await page
    .getByRole("group", { name: "After these steps" })
    .getByLabel("Implement")
    .check();
  await page
    .getByLabel("Rework to", { exact: true })
    .selectOption({ label: "Implement" });
  await page.getByLabel("Maximum rework rounds").fill("4");
  if (await page.getByRole("button", { name: "List", exact: true }).isVisible())
    await page.getByRole("button", { name: "List", exact: true }).click();
  await page
    .getByRole("button", { name: "Move Review up", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("button", { name: "1. Review", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page.waitForURL("**/release-process/edit");
  await expect(
    page.getByRole("heading", { name: "Edit playbook", exact: true }),
  ).toBeVisible();
  const sent = (calls[0].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(sent.steps.map((s) => s.actor.engine)).toEqual(["codex", "claude"]);
  expect(sent.steps[0].rework).toMatchObject({
    to: sent.steps[1].id,
    max_rounds: 4,
  });
  await page.reload();
  await expect(page.getByLabel("Playbook name", { exact: true })).toHaveValue(
    "Release process",
  );
  await page.getByLabel("Playbook name", { exact: true }).fill("Release v2");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  expect(calls.at(-1)?.body.revision).toBe(NEXT);
});

const twoSteps: Flow = {
  format: 2,
  title: "Review loop",
  steps: [
    {
      id: "build",
      title: "Implement",
      actor: { kind: "agent", engine: "missing-agent", model: "missing-model" },
      checklist: [{ key: "built", title: "Built", probe: "supervisor_judged" }],
    },
    {
      id: "review",
      title: "Independent review",
      actor: { kind: "external", label: "Reviewer" },
      checklist: [
        { key: "approved", title: "Approved", probe: "supervisor_judged" },
      ],
    },
  ],
};

async function connect(page: Page, source: string, target: string) {
  const start = page.locator(
    `.react-flow__node[data-id="${source}"] .react-flow__handle[data-handleid="next"]`,
  );
  const end = page.locator(
    `.react-flow__node[data-id="${target}"] .react-flow__handle[data-handleid="after"]`,
  );
  await start.scrollIntoViewIfNeeded();
  const a = await start.boundingBox(),
    b = await end.boundingBox();
  expect(a).toBeTruthy();
  expect(b).toBeTruthy();
  await page.mouse.move(a!.x + a!.width / 2, a!.y + a!.height / 2);
  await page.mouse.down();
  await page.mouse.move(b!.x + b!.width / 2, b!.y + b!.height / 2, {
    steps: 12,
  });
  await page.mouse.up();
}

test("canvas connects the shared draft, refuses cycles and round-trips bounded rework", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "Phones use the equivalent list editor");
  const { calls } = await setup(page, { flow: structuredClone(twoSteps) });
  await page.goto(playbookEditPath("team-flow"));
  await expect(page.getByTestId("playbook-flowchart")).toBeVisible();
  await connect(page, "build", "review");
  await expect(
    page.getByRole("status").filter({ hasText: "Dependency added" }),
  ).toBeVisible();
  await connect(page, "review", "build");
  await expect(
    page.getByRole("status").filter({ hasText: "dependency cycle" }),
  ).toBeVisible();
  await page.locator('.react-flow__node[data-id="review"]').click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Independent review",
  );
  await page.getByLabel("Rework to", { exact: true }).selectOption("build");
  await page.getByLabel("Maximum rework rounds").fill("3");
  await expect(
    page.getByText("Rework: approved · max 3", { exact: true }),
  ).toBeVisible();
  await page.locator('.react-flow__edge[data-id="after:build:review"]').focus();
  await page.keyboard.press("Enter");
  await page
    .getByRole("button", { name: "Remove dependency", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Dependency removed" }),
  ).toBeVisible();
  await expect(
    page.getByText("Rework: approved · max 3", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "List", exact: true }).click();
  await expect(page.getByTestId("playbook-flowchart")).toHaveCount(0);
  const prerequisite = page
    .getByRole("group", { name: "After these steps" })
    .getByLabel("Implement");
  await expect(prerequisite).not.toBeChecked();
  await prerequisite.check();
  await page.getByLabel("Step title", { exact: true }).fill("Final review");
  await page.getByRole("button", { name: "Canvas", exact: true }).click();
  await expect(
    page.locator('.react-flow__node[data-id="review"]'),
  ).toContainText("Final review");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  const saved = (calls[0].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(saved.steps[0]).toEqual(twoSteps.steps[0]);
  expect(saved.steps[1]).toMatchObject({
    title: "Final review",
    after: ["build"],
    rework: { to: "build", when: "approved", max_rounds: 3 },
  });
  expect(JSON.stringify(saved)).not.toMatch(/position|viewport|selected/);
  await page.reload();
  await expect(
    page.getByText("Rework: approved · max 3", { exact: true }),
  ).toBeVisible();
});

test("moving a canvas node is presentation only and does not create an unsaved draft", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "Phones use the equivalent list editor");
  const { calls } = await setup(page, { flow: structuredClone(twoSteps) });
  await page.goto(playbookEditPath("team-flow"));
  const node = page.locator('.react-flow__node[data-id="build"]');
  await node.scrollIntoViewIfNeeded();
  const box = (await node.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + 20);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + 60, { steps: 8 });
  await page.mouse.up();
  await page.getByRole("link", { name: "All playbooks" }).click();
  await expect(
    page.getByRole("dialog", { name: "Leave this playbook?" }),
  ).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`${PLAYBOOKS_PATH}$`));
  expect(calls).toHaveLength(0);
});

test("detail preview is read-only and names the rework bound in canvas and list", async ({
  page,
  isMobile,
}) => {
  const flow = structuredClone(twoSteps);
  flow.steps[1].after = ["build"];
  flow.steps[1].rework = { to: "build", when: "approved", max_rounds: 4 };
  const { calls } = await setup(page, { flow });
  await page.goto(`${PLAYBOOKS_PATH}/team-flow`);
  if (!isMobile) {
    await expect(
      page.getByText("Rework: approved · max 4", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".react-flow__handle.connectable")).toHaveCount(
      0,
    );
    await page.getByRole("button", { name: "List", exact: true }).click();
  }
  await expect(
    page.getByText(
      "Rework to Implement when approved requests changes · at most 4 rounds.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(calls).toHaveLength(0);
});

test("a new playbook with an existing ID can change its ID and save the retained draft", async ({
  page,
}) => {
  const { calls } = await setup(page, { collision: true });
  await page.goto(`${PLAYBOOKS_PATH}/create/new`);
  await page
    .getByLabel("Playbook name", { exact: true })
    .fill("My new process");
  await page.getByLabel("Playbook ID", { exact: true }).fill("team-flow");
  await page.getByLabel("Step title", { exact: true }).fill("My retained step");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByText("A playbook with that ID already exists", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Playbook ID", { exact: true }).fill("available-flow");
  await expect(
    page.getByRole("button", { name: "Save playbook", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`${playbookEditPath("available-flow")}$`),
  );
  await expect(page.getByLabel("Playbook name", { exact: true })).toHaveValue(
    "My new process",
  );
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "My retained step",
  );
  expect(calls).toHaveLength(2);
  expect(calls[1].method).toBe("POST");
  expect(calls[1].body.files["playbook.toml"]).toMatchObject({
    toml: { identity: { id: "available-flow" } },
  });
});

test("a stale save retains the draft, compares current data, and saves only explicitly", async ({
  page,
}) => {
  const { calls } = await setup(page, { stale: true });
  await page.goto(playbookEditPath("team-flow"));
  await page
    .getByLabel("Playbook name", { exact: true })
    .fill("My unsaved name");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Save conflict" }),
  ).toBeVisible();
  await expect(page.getByLabel("Playbook name", { exact: true })).toHaveValue(
    "My unsaved name",
  );
  await page.getByRole("button", { name: "Compare with my draft" }).click();
  await expect(
    page.getByText('"name": "Changed elsewhere"', { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("# New operator comment", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("# original manifest", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "b992c48eafa5b8b91c4e1cbcc8da823f67db7516cbcbf7c57b13a3e6cc84bbfd",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(
    page.getByText(
      "d418c80bd85084221f937a33476da0b5473a7257f2afab92aae137f12fd42df2",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Binary file: 4 bytes", { exact: false }),
  ).toHaveCount(2);
  expect(calls).toHaveLength(1);
  await page.getByRole("button", { name: "Save compared draft" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  expect(calls[1].body.revision).toBe(NEXT);
  expect(calls[1].body.files["template/asset.bin"]).toEqual({
    base64: "AP+A/w==",
  });
  expect(calls[1].body.files["flows/main.toml"]).toBe("# original flow");
});

test("draft copy submits unsaved changes and unavailable actor references", async ({
  page,
}) => {
  const { calls } = await setup(page, { stale: true });
  await page.goto(playbookEditPath("team-flow"));
  await page.getByLabel("Step title", { exact: true }).fill("Unsaved build");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save my draft as new playbook" })
    .click();
  await page.waitForURL("**/draft-copy/edit");
  const sent = (calls[1].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(sent.steps[0].title).toBe("Unsaved build");
  expect(sent.steps[0].actor).toEqual({
    kind: "agent",
    engine: "missing-agent",
    model: "missing-model",
  });
});

test("validation and navigation cancellation retain a draft, then discard loads current", async ({
  page,
}) => {
  await setup(page, { invalid: true, stale: true });
  await page.goto(playbookEditPath("team-flow"));
  await page.getByLabel("Step title", { exact: true }).fill("Keep this draft");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("steps[0].title");
  await page.getByRole("link", { name: "All playbooks" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Cancel", exact: true })
    .click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Keep this draft",
  );
  let prompted = false;
  page.once("dialog", (dialog) => {
    prompted = true;
    void dialog.dismiss();
  });
  await page.reload({ timeout: 1500 }).catch(() => {});
  expect(prompted).toBe(true);
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Keep this draft",
  );
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Discard my draft…", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await expect(page.getByLabel("Playbook name", { exact: true })).toHaveValue(
    "Changed elsewhere",
  );
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Build",
  );
});

test("read-only source cannot enter the editor", async ({ page }) => {
  const { calls } = await setup(page, { readonly: true });
  await page.goto(playbookEditPath("team-flow"));
  await expect(
    page.getByRole("heading", { name: "Playbook cannot be edited here" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Save playbook" })).toHaveCount(
    0,
  );
  expect(calls).toHaveLength(0);
});

test("320px light and dark layout keeps controls in bounds and touch targets usable", async ({
  page,
}) => {
  await setup(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(playbookEditPath("team-flow"));
  await expect(page.getByLabel("Step title", { exact: true })).toBeVisible();
  for (const theme of ["dark", "light"]) {
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme;
    }, theme);
    expect(
      await page
        .getByTestId("playbook-editor")
        .evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true);
    const small = await page
      .getByTestId("playbook-editor")
      .locator("button,input:not([type=checkbox]),select,textarea,a")
      .evaluateAll((els) =>
        els
          .filter((el) => el.getClientRects().length)
          .filter((el) => el.getBoundingClientRect().height < 44)
          .map((el) => el.textContent),
      );
    expect(small).toEqual([]);
  }
});
