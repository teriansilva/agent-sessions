import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { TOUCH_FLOOR } from "./touchTarget";
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
  distinct_role: ["engine", "model", "specialist"],
  grades: ["fast", "strong", "frontier"],
  skills: [
    { id: "forgejo-pr-flow", label: "Forgejo PR flow" },
    { id: "code-review", label: "Code review" },
  ],
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
    invalidField?: string;
    readonly?: boolean;
    collision?: boolean;
    flow?: Flow;
    format?: number;
    legacyAfterRevert?: Flow;
    revertible?: boolean;
    revertRecoveryDurable?: boolean;
  } = {},
) {
  await commonMocks(page);
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: roster.engines } }),
  );
  let revertible = options.revertible ?? false;
  let manifest: Manifest = {
    format: options.format ?? 2,
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
    revertible,
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
    if (path.endsWith("/revert-format")) {
      // #1654: the retained format ≤4 copy comes back; the mock returns the legacy fixture.
      manifest = { ...manifest, format: 2 };
      revertible = false;
      flow = options.legacyAfterRevert ?? flow;
      documents = { ...documents, "playbook.toml": manifest, "flows/main.toml": flow };
      revision = NEXT;
      return route.fulfill({
        json: {
          ...detail(),
          reverted_from: "team-flow-20261024T000000-abcd1234",
          ...(options.revertRecoveryDurable === false
            ? { recovery_durable: false, recovery_reason: "flush failed" }
            : {}),
        },
      });
    }
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
          detail: `flows/main.toml: ${options.invalidField ?? "steps[0].title"} is invalid`,
          field: options.invalidField ?? "steps[0].title",
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

async function selectStep(page: Page, title: string) {
  const back = page.getByRole("button", { name: "Back to flowchart" });
  if (await back.isVisible()) await back.click();
  const node = page.getByLabel(`Step: ${title}`, { exact: true });
  await expect(node).toBeVisible();
  await node.focus();
  await node.press("Enter");
  await expect(
    page.getByRole("region", { name: "Step properties", exact: true }),
  ).toBeVisible();
}

test("create, reorder, add rework, save, reload and give two steps different roles (#1654)", async ({
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
  await page.getByText("Advanced playbook properties", { exact: true }).click();
  await page.getByLabel("Playbook ID", { exact: true }).fill("release-process");
  await selectStep(page, "New step");
  await page.getByLabel("Step title", { exact: true }).fill("Implement");
  await page
    .getByLabel("Who does this step?", { exact: true })
    .selectOption("agent");
  // A new playbook is format 5: an agent step names a ROLE, never an agent or model.
  await expect(page.getByLabel("Agent", { exact: true })).toHaveCount(0);
  await page.getByLabel("Add a skill", { exact: true }).selectOption("forgejo-pr-flow");
  await page.getByRole("radio", { name: "Specialist", exact: true }).click();
  await page.getByRole("button", { name: "Add step", exact: true }).click();
  await page.getByLabel("Step title", { exact: true }).fill("Review");
  await page
    .getByLabel("Who does this step?", { exact: true })
    .selectOption("agent");
  await page.getByLabel("Add a skill", { exact: true }).selectOption("code-review");
  await page.getByRole("radio", { name: "Senior", exact: true }).click();
  await page
    .getByRole("group", { name: "Starts after" })
    .getByLabel("Implement")
    .check();
  await page
    .getByLabel("Rework to", { exact: true })
    .selectOption({ label: "Implement" });
  await page.getByLabel("Maximum revision rounds").fill("4");
  await page
    .getByRole("button", { name: "Move Review up", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("status").filter({ hasText: "Step 1 of 2: Review" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page.waitForURL("**/release-process/edit");
  await expect(
    page.getByRole("heading", { name: "Edit playbook", exact: true }),
  ).toBeVisible();
  const sent = (calls[0].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(sent.format).toBe(5);
  expect(sent.steps.map((s) => s.actor)).toEqual([{ kind: "agent" }, { kind: "agent" }]);
  expect(sent.steps.map((s) => s.role)).toEqual([
    { skills: ["code-review"], min_grade: "frontier" },
    { skills: ["forgejo-pr-flow"], min_grade: "strong" },
  ]);
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
  test.skip(
    isMobile,
    "Pointer dragging is covered here; mobile uses the non-drag controls below",
  );
  const { calls } = await setup(page, { flow: structuredClone(twoSteps) });
  await page.goto(playbookEditPath("team-flow"));
  await expect(page.getByTestId("playbook-flowchart")).toBeVisible();
  await connect(page, "build", "review");
  await expect(
    page.getByRole("status").filter({ hasText: "Dependency added" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Fit flow", exact: true }).click();
  await connect(page, "review", "build");
  await expect(
    page.getByRole("status").filter({ hasText: "dependency cycle" }),
  ).toBeVisible();
  await page.locator('.react-flow__node[data-id="review"]').click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Independent review",
  );
  await page.getByLabel("Rework to", { exact: true }).selectOption("build");
  await page.getByLabel("Maximum revision rounds").fill("3");
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
  await expect(page.getByTestId("playbook-flowchart")).toBeVisible();
  const prerequisite = page
    .getByRole("group", { name: "Starts after" })
    .getByLabel("Implement");
  await expect(prerequisite).not.toBeChecked();
  await prerequisite.check();
  await page.getByLabel("Step title", { exact: true }).fill("Final review");
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
  test.skip(
    isMobile,
    "Pointer dragging is covered here; mobile uses the non-drag controls below",
  );
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
  await page.getByText("Advanced playbook properties", { exact: true }).click();
  await page.getByLabel("Playbook ID", { exact: true }).fill("team-flow");
  await selectStep(page, "New step");
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
  await selectStep(page, "My retained step");
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
  await selectStep(page, "Build");
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
  await selectStep(page, "Build");
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
  await selectStep(page, "Build");
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Build",
  );
});

test("AI authoring lives in HQ: no dead AI button, and Ask HQ opens HQ (#1611)", async ({
  page,
}) => {
  await setup(page);
  await page.goto(playbookEditPath("team-flow"));
  await expect(page.getByRole("button", { name: "Save playbook" })).toBeVisible();
  await expect(page.getByText("Create workflow with AI")).toHaveCount(0);
  await expect(page.getByText("AI creation is not available yet.")).toHaveCount(0);
  const ask = page.getByTestId("playbook-ask-hq").getByRole("button", { name: "Ask HQ" });
  expect((await ask.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await ask.click();
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
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
  await selectStep(page, "Build");
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

test("workflow selection never falls back to its first step", async ({
  page,
  isMobile,
}) => {
  const { calls } = await setup(page);
  await page.goto(playbookEditPath("team-flow"));
  const workflow = page.getByRole("region", {
    name: "Workflow properties",
    exact: true,
  });
  await expect(workflow).toBeVisible();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "List", exact: true }),
  ).toHaveCount(0);
  await selectStep(page, "Build");
  await page.getByLabel("Step instructions").fill("Keep these instructions");
  await page.getByLabel("Evidence required").fill("Keep the required evidence");
  await page.keyboard.press("Escape");
  await expect(workflow).toBeVisible();
  await expect(page.getByLabel("Step: Build", { exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(workflow).toBeVisible();
  await selectStep(page, "Build");
  await expect(page.getByLabel("Step instructions")).toHaveValue(
    "Keep these instructions",
  );
  await expect(page.getByLabel("Evidence required")).toHaveValue(
    "Keep the required evidence",
  );
  if (!isMobile) {
    await page.locator(".react-flow__pane").click({ position: { x: 8, y: 8 } });
    await expect(workflow).toBeVisible();
  }
  expect(calls).toHaveLength(0);
});

test("320px graph supports connecting, reordering and confirmed removal without dragging", async ({
  page,
}) => {
  const { calls } = await setup(page, { flow: structuredClone(twoSteps) });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(playbookEditPath("team-flow"));
  await expect(page.getByTestId("playbook-flowchart")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "1. Implement", exact: true }),
  ).toHaveCount(0);
  await selectStep(page, "Independent review");
  await page
    .getByRole("group", { name: "Starts after", exact: true })
    .getByLabel("Implement", { exact: true })
    .check();
  await page.getByLabel("Rework to", { exact: true }).selectOption("build");
  await page.getByLabel("Maximum revision rounds").fill("4");
  await page
    .getByRole("button", { name: "Move Independent review up", exact: true })
    .press("Enter");
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Step 1 of 2: Independent review" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Remove step…", exact: true })
    .press("Enter");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Cancel", exact: true })
    .press("Enter");
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Independent review",
  );
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  const saved = (calls[0].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(saved.steps[0]).toMatchObject({
    id: "review",
    after: ["build"],
    rework: { to: "build", when: "approved", max_rounds: 4 },
  });
  expect(saved.steps[1]).toEqual(twoSteps.steps[0]);
  await selectStep(page, "Independent review");
  await page
    .getByRole("button", { name: "Remove step…", exact: true })
    .press("Enter");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .press("Enter");
  await expect(page.getByTestId("playbook-flowchart")).toBeVisible();
  await expect(
    page.getByLabel("Step: Independent review", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Step title", { exact: true })).toHaveCount(0);
});

test("server validation opens the correct step and its collapsed Advanced group", async ({
  page,
}) => {
  await setup(page, {
    flow: structuredClone(twoSteps),
    invalid: true,
    invalidField: "steps[0].memory",
  });
  await page.goto(playbookEditPath("team-flow"));
  await selectStep(page, "Independent review");
  await page
    .getByLabel("Step instructions")
    .fill("Retain my review instructions");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Go to field: steps[0].memory", exact: true })
    .click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Implement",
  );
  await expect(page.getByLabel("Shared memory", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Shared memory", { exact: true })).toBeFocused();
  await expect(
    page.getByLabel("Shared memory", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true");
  await selectStep(page, "Independent review");
  await expect(page.getByLabel("Step instructions")).toHaveValue(
    "Retain my review instructions",
  );
});

test("an empty workflow validation error focuses the graph for adding its first step", async ({
  page,
}) => {
  await setup(page, {
    flow: { format: 1, title: "Empty workflow", steps: [] },
    invalid: true,
    invalidField: "steps",
  });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(playbookEditPath("team-flow"));
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Go to field: steps", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Flowchart", exact: true }),
  ).toBeFocused();
  await page.getByRole("button", { name: "Add step", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Step properties", exact: true }),
  ).toBeVisible();
});

test("a variable error returns from step Properties and expands workflow inputs", async ({
  page,
}) => {
  await setup(page, { invalid: true, invalidField: "variables[0].name" });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(playbookEditPath("team-flow"));
  await page.getByText("Variables (0)", { exact: true }).click();
  await page.getByRole("button", { name: "Add variable", exact: true }).click();
  await page.getByLabel("Variable name", { exact: true }).fill("repository");
  await selectStep(page, "Build");
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Go to field: variables[0].name",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("region", { name: "Workflow properties", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Variable name", { exact: true })).toBeFocused();
  await expect(page.getByLabel("Variable name", { exact: true })).toHaveValue(
    "repository",
  );
});

test("Back preserves graph viewport, workflow switching retains drafts, and empty workflows have no implicit step", async ({
  page,
}) => {
  await setup(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(playbookEditPath("team-flow"));
  const viewport = page.locator(".react-flow__viewport");
  const initial = await viewport.getAttribute("style");
  await page
    .getByRole("button", { name: "Zoom out flowchart", exact: true })
    .click();
  await expect(viewport).not.toHaveAttribute("style", initial!);
  const zoomed = await viewport.getAttribute("style");
  await selectStep(page, "Build");
  await page.getByLabel("Step instructions").fill("Keep this step draft");
  await page.getByRole("button", { name: "Back to flowchart" }).click();
  await expect(page.getByLabel("Step: Build", { exact: true })).toBeFocused();
  await expect(viewport).toHaveAttribute("style", zoomed!);
  await page.getByRole("button", { name: "Add workflow", exact: true }).click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("Add a step to start the flow.", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Workflow title", { exact: true })
    .fill("Second workflow");
  await page.getByRole("button", { name: "Add step", exact: true }).click();
  await page.getByLabel("Step title", { exact: true }).fill("Second step");
  await page
    .getByLabel("Workflow", { exact: true })
    .selectOption("flows/main.toml");
  await expect(page.getByLabel("Step title", { exact: true })).toHaveCount(0);
  await selectStep(page, "Build");
  await expect(page.getByLabel("Step instructions")).toHaveValue(
    "Keep this step draft",
  );
  await page
    .getByLabel("Workflow", { exact: true })
    .selectOption({ label: "Second workflow" });
  await selectStep(page, "Second step");
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Second step",
  );
});

test("mobile graph keeps nodes readable and offers navigation without panning", async ({
  page,
}) => {
  await setup(page, { flow: structuredClone(twoSteps) });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(playbookEditPath("team-flow"));
  const chart = page.getByTestId("playbook-flowchart");
  await chart.scrollIntoViewIfNeeded();
  const first = page.getByLabel("Step: Implement", { exact: true });
  await expect(first).toBeVisible();
  await expect
    .poll(async () => (await first.boundingBox())?.width ?? 0)
    .toBeGreaterThanOrEqual(168);
  await page
    .getByRole("button", { name: "Show next step", exact: true })
    .click();
  const next = page.getByLabel("Step: Independent review", { exact: true });
  await expect(next).toBeInViewport();
  await next.click();
  await expect(page.getByLabel("Step title", { exact: true })).toHaveValue(
    "Independent review",
  );
  await page.getByRole("button", { name: "Back to flowchart" }).click();
  await expect(
    page.getByRole("button", { name: "Show previous step", exact: true }),
  ).toBeEnabled();
});

test("workflow and Escape actions clear edge selection without deleting the dependency", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "The narrow-screen equivalent uses Starts after");
  const flow = structuredClone(twoSteps);
  flow.steps[1].after = ["build"];
  const { calls } = await setup(page, { flow });
  await page.goto(playbookEditPath("team-flow"));
  const edge = page.locator('.react-flow__edge[data-id="after:build:review"]');
  await edge.focus();
  await edge.press("Enter");
  await expect(
    page.getByRole("button", { name: "Remove dependency", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Workflow properties", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Remove dependency", exact: true }),
  ).toHaveCount(0);
  await edge.focus();
  await edge.press("Enter");
  await edge.press("Escape");
  await expect(
    page.getByRole("button", { name: "Remove dependency", exact: true }),
  ).toHaveCount(0);
  await expect(edge).toHaveCount(1);
  expect(calls).toHaveLength(0);
});

/* ---- #1654: format 5 roles, the explicit legacy upgrade, and Revert to format 4 ---------------- */

const roleFlow: Flow = {
  format: 5,
  title: "Ship a change",
  steps: [
    {
      id: "review",
      title: "Review",
      actor: { kind: "agent" },
      role: {
        skills: ["code-review", "security-audit"],
        min_grade: "frontier",
      },
      checklist: [
        { key: "reviewed", title: "Reviewed", probe: "supervisor_judged" },
      ],
    },
  ],
};

test("a format 5 step edits its role: library skills by id, a missing skill flagged, a grade", async ({
  page,
}) => {
  const { calls } = await setup(page, { format: 5, flow: roleFlow });
  await page.goto(playbookEditPath("team-flow"));
  await selectStep(page, "Review");
  const role = page.getByTestId("step-role");
  await expect(role.getByText("Not in your skills library")).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "Senior", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await role
    .getByRole("button", { name: "Remove skill security-audit" })
    .click();
  await page.getByRole("radio", { name: "Specialist", exact: true }).click();
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  const sent = (calls[0].body.files["flows/main.toml"] as { toml: Flow }).toml;
  expect(sent.steps[0].actor).toEqual({ kind: "agent" });
  expect(sent.steps[0].role).toEqual({
    skills: ["code-review"],
    min_grade: "strong",
  });
});

test("a format 4 step shows its recorded agent read-only and upgrades only by explicit choice", async ({
  page,
}) => {
  const legacyFlow: Flow = {
    format: 2,
    title: "Main flow",
    steps: [
      {
        id: "build",
        title: "Build",
        actor: { kind: "agent", engine: "codex", model: "default" },
        skills: [
          { id: "forgejo-pr-flow", required: true },
          { id: "code.review", required: false },
          { id: "code-review", required: false },
        ],
        checklist: [
          { key: "built", title: "Built", probe: "supervisor_judged" },
        ],
      },
      {
        id: "check",
        title: "Check",
        after: ["build"],
        actor: { kind: "agent", engine: "claude", model: "default" },
        skills: [{ id: "code-review", required: true }],
      },
      {
        id: "approve",
        title: "Approve",
        after: ["check"],
        actor: { kind: "operator" },
        skills: [{ id: "release-sign-off" }],
      },
    ],
  };
  const { calls } = await setup(page, { flow: legacyFlow });
  await page.goto(playbookEditPath("team-flow"));
  await selectStep(page, "Build");
  const legacy = page.getByTestId("legacy-actor");
  await expect(legacy).toContainText("Format 4 step");
  await expect(page.getByLabel("Agent", { exact: true })).toHaveCount(0);
  await legacy
    .getByRole("button", { name: /Upgrade this playbook to format 5/ })
    .click();
  const upgrade = page.getByRole("group", { name: "Upgrade to format 5" });
  // Hermes 6947 #4: the upgrade changes EVERY agent step, so every one is shown and confirmed.
  const groups = upgrade.getByTestId("upgrade-step");
  await expect(groups).toHaveCount(3);
  const build = groups.filter({ hasText: "Build" });
  const check = groups.filter({ hasText: "Check" });
  // Required is pre-selected; advisory is NOT; a blocked id is disabled and says how to fix it.
  await expect(build.getByRole("checkbox").nth(0)).toBeChecked();
  await expect(build.getByRole("checkbox").nth(1)).toBeDisabled();
  await expect(build).toContainText("Rename “code.review” to “code-review”");
  await expect(build.getByRole("checkbox").nth(2)).not.toBeChecked();
  // The other step's required reference is visible and pre-selected — and the operator's to drop.
  await expect(check.getByRole("checkbox")).toBeChecked();
  await check.getByRole("checkbox").uncheck();
  // Hermes 6955 #3: a non-agent step's references cannot migrate; removing them is acknowledged.
  const approve = groups.filter({ hasText: "Approve" });
  await expect(approve).toContainText("references are removed");
  await expect(approve).toContainText("release-sign-off");
  await expect(approve).toContainText("main:approve");
  const go = upgrade.getByRole("button", { name: "Upgrade to format 5", exact: true });
  await expect(go).toBeDisabled();
  await upgrade.getByRole("checkbox", { name: /Remove the skill references/ }).check();
  await expect(go).toBeEnabled();
  await upgrade
    .getByRole("button", { name: "Upgrade to format 5", exact: true })
    .click();
  await expect(page.getByTestId("step-role")).toBeVisible();
  await page
    .getByRole("button", { name: "Save playbook", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Playbook saved" }),
  ).toBeVisible();
  const files = calls[0].body.files;
  const manifestSent = (files["playbook.toml"] as { toml: Manifest }).toml;
  const sent = (files["flows/main.toml"] as { toml: Flow }).toml;
  expect(manifestSent.format).toBe(5);
  expect(sent.format).toBe(5);
  expect(sent.steps[0].actor).toEqual({ kind: "agent" });
  expect(sent.steps[0].role).toEqual({ skills: ["forgejo-pr-flow"] });
  expect(sent.steps[0].skills).toBeUndefined();
  // Unchecked in the confirm: never promoted.
  expect(sent.steps[1].actor).toEqual({ kind: "agent" });
  expect(sent.steps[1].role).toEqual({ skills: [] });
  expect(sent.steps[1].skills).toBeUndefined();
  expect(sent.steps[2].actor).toEqual({ kind: "operator" });
  expect(sent.steps[2].skills).toBeUndefined();
  expect(sent.steps[2].role).toBeUndefined();
});

test("Revert to format 4 is confirmed, revision-fenced and adopts the restored copy", async ({
  page,
}) => {
  const legacyFlow: Flow = {
    format: 2,
    title: "Main flow",
    steps: [
      {
        id: "build",
        title: "Build",
        actor: { kind: "agent", engine: "codex", model: "default" },
        checklist: [
          { key: "built", title: "Built", probe: "supervisor_judged" },
        ],
      },
    ],
  };
  const { calls } = await setup(page, {
    format: 5,
    flow: roleFlow,
    legacyAfterRevert: legacyFlow,
    revertible: true,
  });
  await page.goto(playbookEditPath("team-flow"));
  await page
    .getByRole("button", { name: "Revert to format 4…", exact: true })
    .click();
  await page.getByRole("button", { name: "Revert", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Reverted to the retained format 4 copy" }),
  ).toBeVisible();
  expect(calls.at(-1)?.body).toEqual({ revision: REV });
  await selectStep(page, "Build");
  await expect(page.getByTestId("legacy-actor")).toContainText("Format 4 step");
  await expect(
    page.getByRole("button", { name: "Revert to format 4…" }),
  ).toHaveCount(0);
});
test("a revert whose format 5 recovery copy is unconfirmed says so (#1654)", async ({
  page,
}) => {
  const legacyFlow: Flow = {
    format: 2,
    title: "Main flow",
    steps: [{ id: "build", title: "Build", actor: { kind: "agent", engine: "codex", model: "default" } }],
  };
  await setup(page, {
    format: 5,
    flow: roleFlow,
    legacyAfterRevert: legacyFlow,
    revertible: true,
    revertRecoveryDurable: false,
  });
  await page.goto(playbookEditPath("team-flow"));
  await page.getByRole("button", { name: "Revert to format 4…", exact: true }).click();
  await page.getByRole("button", { name: "Revert", exact: true }).click();
  const notice = page.getByRole("status").filter({ hasText: "Reverted to the retained format 4 copy" });
  await expect(notice).toContainText("could not be confirmed");
  await expect(notice).not.toContainText("is kept in the recovery area");
});
test("a format 5 book with no retained format 4 copy offers no revert (#1654)", async ({
  page,
}) => {
  // Hermes 6947 #8: a new or duplicated format 5 book has nothing to revert to.
  await setup(page, { format: 5, flow: roleFlow, revertible: false });
  await page.goto(playbookEditPath("team-flow"));
  await expect(
    page.getByRole("button", { name: "Save playbook", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Revert to format 4…" }),
  ).toHaveCount(0);
});

test("320px: the role editor stays in bounds with 44px targets in light and dark", async ({
  page,
}) => {
  await setup(page, { format: 5, flow: roleFlow });
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto(playbookEditPath("team-flow"));
  await selectStep(page, "Review");
  for (const theme of ["dark", "light"]) {
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, theme);
    const editor = page.getByTestId("playbook-editor");
    expect(
      await editor.evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true);
    const small = await page
      .getByTestId("step-role")
      .locator("button,select")
      .evaluateAll((els) =>
        els
          .filter((el) => el.getClientRects().length)
          .filter((el) => el.getBoundingClientRect().height < 44)
          .map((el) => el.textContent),
      );
    expect(small).toEqual([]);
  }
});
