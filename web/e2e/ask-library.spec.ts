/** HQ's Library cards (#1611), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that the card fetches and shows what it saves, verbatim, and
 *  folds a long file behind a "Show all" control the operator can reach by keyboard; that an edit's
 *  diff keeps its + / − signs; that the automation consent card shows every scope line and the
 *  whole instruction before Enable; that a done save links into the Library; that a card whose
 *  draft changed says so; that every control meets the 44px floor and nothing scrolls sideways on
 *  a phone.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  decideRoute,
  minTarget,
  noSidewaysScroll,
  open,
  PID,
  shell,
} from "./askOperationShell";

const README = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "playbook.create",
    risk: "reversible",
    label: "Create playbook",
    verb: "Save playbook",
    title: "Release train",
    text: "Saves a new playbook, “Release train” (2 files), to your Library. It doesn't run, deploy or start anything.",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    content: true,
    ...over,
  };
}

async function content(page: Page, body: Record<string, unknown>, status = 200) {
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) =>
    r.fulfill({ status, json: body }),
  );
}

const FILES = {
  kind: "files",
  files: [
    { path: "playbook.toml", text: 'format = 1\n\n[identity]\nid = "release-train"' },
    { path: "README.md", text: README },
  ],
};

test("a playbook card shows its files verbatim, folds a long one, and links the save (#1611)", async ({
  page,
}) => {
  const fake = await shell(page, card());
  await content(page, FILES);
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Saved to your Library. Open it in the Library.",
    library: { kind: "playbook", id: "release-train" },
  });
  const c = await open(page);
  await expect(c).toContainText("Playbook");
  const files = c.getByTestId("ask-library-file");
  await expect(files.first()).toContainText('id = "release-train"');
  const readme = files.nth(1);
  await expect(readme).toContainText("line 12");
  await expect(readme).not.toContainText("line 13");
  const more = c.getByTestId("ask-library-more");
  await expect(more).toHaveText("Show all 30 lines");
  await minTarget(more);
  await more.focus();
  await page.keyboard.press("Enter");
  await expect(readme).toContainText("line 30");
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await noSidewaysScroll(page);

  await c.getByTestId("ask-operation-confirm").click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
    timeout: 20_000,
  });
  expect(bodies).toEqual([{ decision: "confirm" }]);
  const link = c.getByTestId("ask-library-open");
  await minTarget(link);
  await expect(link).toHaveAttribute("href", "/library/playbooks/release-train");
});

test("an edit card shows the diff with its signs, not colour alone (#1611)", async ({ page }) => {
  await shell(page, card({ op: "playbook.edit", label: "Edit playbook", verb: "Save changes" }));
  await content(page, {
    kind: "diff",
    diff: [
      {
        path: "flows/release.toml",
        status: "changed",
        added: 1,
        removed: 1,
        diff: '@@ -1,2 +1,2 @@\n id = "staging"\n-after = ["changelog"]\n+after = ["changelog", "ci"]',
      },
    ],
  });
  const c = await open(page);
  const diff = c.getByTestId("ask-library-diff");
  await expect(diff).toContainText('-after = ["changelog"]');
  await expect(diff).toContainText('+after = ["changelog", "ci"]');
  await expect(c).toContainText("changed · +1 −1");
  await noSidewaysScroll(page);
});

test("the enable card is a consent card: every scope line and the whole instruction (#1611)", async ({
  page,
}) => {
  const instruction = Array.from({ length: 16 }, (_, i) => `step ${i + 1}`).join("\n");
  await shell(
    page,
    card({
      op: "automation.enable",
      risk: "external",
      label: "Enable",
      verb: "Enable",
      title: "Nightly dependency check",
      text: "Turns it on. From then it starts agents by itself, within exactly the scope below.",
    }),
  );
  await content(page, {
    kind: "automation",
    name: "Nightly dependency check",
    changed: [],
    scope_lines: ["Runs every day at 02:00 (UTC)", "Starts a mission in web", "Permission bypass: off"],
    instruction,
    status: "enable",
    widened: [],
  });
  const c = await open(page);
  await expect(c).toContainText("Runs agents unattended");
  for (const line of ["Runs every day at 02:00 (UTC)", "Starts a mission in web", "Permission bypass: off"])
    await expect(c.getByTestId("ask-library-automation")).toContainText(line);
  await c.getByTestId("ask-library-more").click();
  await expect(c.getByTestId("ask-library-instruction")).toContainText("step 16");
  await expect(c.getByTestId("ask-library-status")).toContainText("starts agents by itself");
  await minTarget(c.getByTestId("ask-operation-confirm"));
  await noSidewaysScroll(page);
});

test("an edit of an enabled automation names what changes and says it remains enabled (#1611)", async ({ page }) => {
  await shell(page, card({ op: "automation.edit", label: "Edit automation", verb: "Save changes" }));
  await content(page, {
    kind: "automation",
    name: "Weekly audit",
    changed: ["name", "trigger"],
    scope_lines: ["Runs every day at 02:00 (UTC)"],
    instruction: "Audit the dependencies.",
    status: "enabled",
    widened: [],
  });
  const c = await open(page);
  await expect(c.getByTestId("ask-library-settings")).toContainText("Weekly audit");
  await expect(c.getByTestId("ask-library-settings")).toContainText("name, trigger");
  await expect(c.getByTestId("ask-library-status")).toHaveText(
    "Remains enabled: changes affect its next and later runs.",
  );
});

test("a card whose draft changed after it was proposed says so and can't be confirmed (#1611)", async ({
  page,
}) => {
  await shell(page, card());
  await content(page, { detail: "this card's content changed after it was proposed" }, 409);
  const c = await open(page);
  await expect(c.getByTestId("ask-library-error")).toContainText("changed after it was proposed");
  await expect(c.getByTestId("ask-operation-confirm")).toBeDisabled();
  await expect(c.getByTestId("ask-library-retry")).toHaveCount(0);
});

test("Confirm waits until what it saves is on screen (#1611 review)", async ({ page }) => {
  await shell(page, card());
  let release: () => void = () => {};
  const held = new Promise<void>((r) => (release = r));
  await page.route(`**/api/ask/proposals/${PID}/content`, async (r) => {
    await held;
    await r.fulfill({ json: FILES });
  });
  const c = await open(page);
  await expect(c.getByTestId("ask-library-loading")).toBeVisible();
  await expect(c.getByTestId("ask-operation-confirm")).toBeDisabled();
  release();
  await expect(c.getByTestId("ask-library-files")).toBeVisible();
  await expect(c.getByTestId("ask-operation-confirm")).toBeEnabled();
});

test("a content read that failed offers Retry and Confirm only after it succeeds (#1611 review)", async ({
  page,
}) => {
  await shell(page, card());
  let calls = 0;
  await page.route(`**/api/ask/proposals/${PID}/content`, (r) =>
    ++calls === 1
      ? r.fulfill({ status: 503, json: { detail: "the proposal could not be read just now" } })
      : r.fulfill({ json: FILES }),
  );
  const c = await open(page);
  await expect(c.getByTestId("ask-library-error")).toContainText("can't be confirmed yet");
  await expect(c.getByTestId("ask-operation-confirm")).toBeDisabled();
  const retry = c.getByTestId("ask-library-retry");
  await minTarget(retry);
  await retry.click();
  await expect(c.getByTestId("ask-library-files")).toBeVisible();
  await expect(c.getByTestId("ask-operation-confirm")).toBeEnabled();
});
