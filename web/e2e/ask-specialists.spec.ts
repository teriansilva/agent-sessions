/** HQ's specialist cards (#1938), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that a new specialist's seven rows are on screen before the
 *  CTA can be tapped; that an edit's diff keeps its − / + signs and puts the changed rows first;
 *  that archive is never the danger fill; that a playbook card lists the specialists it relies on
 *  above its files and still offers Save; that an out-of-date card keeps its values, marks the pin
 *  that moved and offers "Ask HQ again", which sends an ordinary message; that a store refusal is
 *  a warning, not a failure; that six cards in one turn fold to rows with no CTA until one is
 *  opened; and that every control meets the 44px floor in keyboard order, with nothing scrolling
 *  sideways on a phone.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { minTarget, noSidewaysScroll, open, shell } from "./askOperationShell";
import { openAsk, pickConversation } from "./askSidebar";
import { ASK_STREAM, fulfillAsk, turnOf, type FakeAsk } from "./askStream";

const id = (n: number) => n.toString(16).repeat(32).slice(0, 32);

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: id(1),
    kind: "operation",
    op: "specialist.create",
    risk: "reversible",
    label: "New specialist",
    verb: "Create specialist",
    title: "Release Notes Writer",
    text: "Creates the specialist Release Notes Writer in your Library.",
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

const WRITER = {
  title: "Release Notes Writer",
  summary: "Writes the release notes for a tagged release from the merged pull requests.",
  engine: "claude",
  agent: "Claude Code",
  model: "claude-sonnet-4-5",
  effort: "medium",
  grade: "strong",
  skills: [
    { id: "changelog-style", revision: "sha256:aaaa1111bbbb2222" },
    { id: "release-notes", revision: "sha256:cccc3333dddd4444" },
    { id: "git-history", revision: "sha256:eeee5555ffff6666" },
  ],
  capacity: 3,
};
const NOW = Object.fromEntries(WRITER.skills.map((k) => [k.id, k.revision]));
const CREATE = { kind: "specialist", action: "create", after: WRITER, before: null, skills_now: NOW };

const REVIEWER = {
  title: "Code Reviewer",
  summary: "Reviews a pull request for correctness, tests and security before it merges.",
  engine: "claude",
  agent: "Claude Code",
  model: "claude-sonnet-4-5",
  effort: "high",
  grade: "strong",
  skills: [
    { id: "review-checklist", revision: "sha256:11112222" },
    { id: "security-review", revision: "sha256:33334444" },
  ],
  capacity: 2,
};
const EDIT = {
  kind: "specialist",
  action: "edit",
  before: REVIEWER,
  after: {
    ...REVIEWER,
    model: "claude-opus-4-5",
    grade: "frontier",
    capacity: 6,
    skills: [...REVIEWER.skills, { id: "test-plan", revision: "sha256:55556666" }],
  },
  skills_now: {},
  version: { from: 3, to: 4 },
};

async function content(page: Page, pid: string, body: Record<string, unknown>) {
  await page.route(`**/api/ask/proposals/${pid}/content`, (r) => r.fulfill({ json: body }));
}

/** The decide route for any card of the one conversation: records the tap, settles the card. */
async function decide(
  fake: FakeAsk,
  page: Page,
  settle: (pid: string) => Record<string, unknown>,
) {
  const taps: { pid: string; decision: unknown }[] = [];
  await page.route(/\/api\/ask\/proposals\/([0-9a-f]{32})\/decide$/, (r) => {
    const pid = /proposals\/([0-9a-f]{32})\/decide/.exec(r.request().url())![1];
    const body = r.request().postDataJSON() as Record<string, unknown>;
    taps.push({ pid, decision: body.decision });
    const p = fake.conversations[0].turns
      .flatMap((t) => t.proposals ?? [])
      .find((x) => x.action_id === pid)!;
    const done =
      body.decision === "dismiss"
        ? { state: "dismissed" }
        : settle(pid);
    Object.assign(p, { ...done, can_confirm: false, can_dismiss: false });
    return r.fulfill({
      status: body.decision === "dismiss" ? 200 : 202,
      json: { proposal: { state: body.decision === "dismiss" ? "dismissed" : "deciding" } },
    });
  });
  return taps;
}

async function several(page: Page, cards: Record<string, unknown>[]): Promise<FakeAsk> {
  const fake = await shell(page, cards[0]);
  fake.conversations[0].turns[0].proposals = cards;
  return fake;
}

/** The conversation with several cards open (`open` expects exactly one). */
async function openAll(page: Page): Promise<void> {
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-operation").first()).toBeVisible();
}

async function rowTexts(c: Locator): Promise<string[]> {
  return c.getByTestId("ask-specialist-row").evaluateAll((els) =>
    els.map((el) => el.getAttribute("data-field") ?? ""),
  );
}

test("a new specialist shows all seven rows before Create, and the tap creates it (#1938)", async ({
  page,
}) => {
  const fake = await shell(page, card());
  await content(page, id(1), CREATE);
  const taps = await decide(fake, page, () => ({
    state: "done",
    summary: "Created. Open it in the Library.",
    library: { kind: "specialist", id: "" },
  }));
  const c = await open(page);
  await expect(c).toContainText("Specialist");
  await expect(c).toContainText("Reversible");
  await expect(c).toContainText("New specialist");
  await expect(c).toContainText("The specialist, in full");
  expect(await rowTexts(c)).toEqual([
    "summary",
    "agent",
    "model",
    "effort",
    "grade",
    "skills",
    "capacity",
  ]);
  const rows = c.getByTestId("ask-specialist-row");
  await expect(rows.nth(1)).toContainText("Claude Code");
  await expect(rows.nth(2)).toContainText("claude-sonnet-4-5");
  await expect(rows.nth(3)).toContainText("Medium");
  // The grade is a chip and a fixed hint, never a control.
  await expect(c.getByTestId("ask-specialist-grade")).toHaveText("Specialist");
  await expect(rows.nth(4)).toContainText("Follows the model, from your model grades. Never chosen here.");
  await expect(rows.nth(4).locator("input, select, button")).toHaveCount(0);
  await expect(c.getByTestId("ask-specialist-skill")).toHaveText([
    "changelog-style · rev aaaa1111",
    "release-notes · rev cccc3333",
    "git-history · rev eeee5555",
  ]);
  await expect(rows.nth(6)).toHaveText("CapacityUp to 3 at once");
  await noSidewaysScroll(page);

  // Keyboard order: the CTA, then Dismiss; both at the floor; the CTA is the shared primary.
  const cta = c.getByTestId("ask-operation-confirm");
  const dismiss = c.getByTestId("ask-operation-dismiss");
  await expect(cta).toHaveText("Create specialist");
  await expect(cta).toBeEnabled();
  await minTarget(cta);
  await minTarget(dismiss);
  await cta.focus();
  await page.keyboard.press("Tab");
  await expect(dismiss).toBeFocused();
  await cta.focus();
  await page.keyboard.press("Enter");
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", { timeout: 20_000 });
  expect(taps).toEqual([{ pid: id(1), decision: "confirm" }]);
  const link = c.getByTestId("ask-library-open");
  await minTarget(link);
  await expect(link).toHaveAttribute("href", "/library/specialists");
});

test("an edit is a per-field diff: changed rows first, signs kept, the grade follows (#1938)", async ({
  page,
}) => {
  await shell(
    page,
    card({
      op: "specialist.edit",
      label: "Edit specialist",
      verb: "Save as version 4",
      title: "Code Reviewer · version 3 → 4",
      text: "Saves version 4 of Code Reviewer. Missions that pinned version 3 keep it.",
    }),
  );
  await content(page, id(1), EDIT);
  const c = await open(page);
  await expect(c).toContainText("Code Reviewer · version 3 → 4");
  await expect(c.getByTestId("ask-specialist-counts")).toHaveText(
    "Changes3 changed1 follows4 unchanged",
  );
  expect(await rowTexts(c)).toEqual([
    "model",
    "grade",
    "skills",
    "capacity",
    "title",
    "summary",
    "agent",
    "effort",
  ]);
  const rows = c.getByTestId("ask-specialist-row");
  await expect(rows.nth(0)).toContainText("−claude-sonnet-4-5");
  await expect(rows.nth(0)).toContainText("+claude-opus-4-5");
  await expect(rows.nth(1)).toHaveAttribute("data-state", "follows");
  await expect(rows.nth(1)).toContainText("−Specialist");
  await expect(rows.nth(1)).toContainText("+Senior");
  await expect(rows.nth(2).getByTestId("ask-specialist-skill")).toHaveText([
    "review-checklist · rev 11112222",
    "security-review · rev 33334444",
    "+test-plan · rev 55556666",
  ]);
  await expect(rows.nth(3)).toContainText("−Up to 2 at once");
  await expect(rows.nth(3)).toContainText("+Up to 6 at once");
  await expect(rows.nth(4)).toHaveAttribute("data-state", "same");
  // A changed row is told apart by its edge and label as well as its signs; an unchanged row is
  // quiet text, never faded.
  expect(await rows.nth(0).evaluate((el) => getComputedStyle(el).borderLeftWidth)).toBe("2px");
  expect(await rows.nth(4).evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
  await expect(c.getByTestId("ask-operation-confirm")).toHaveText("Save as version 4");
  await noSidewaysScroll(page);
});

test("archive and unarchive say what the tap does not do, and are never the danger fill (#1938)", async ({
  page,
}) => {
  const archive = card({
    op: "specialist.archive",
    label: "Archive specialist",
    verb: "Archive",
    title: "QA Runner · version 2",
    text: "No new launches. Versions that missions pinned stay readable.",
    content: undefined,
  });
  const unarchive = card({
    action_id: id(2),
    op: "specialist.unarchive",
    label: "Unarchive specialist",
    verb: "Unarchive",
    title: "Docs Editor · version 5 · archived",
    text: "It can be launched again. Nothing about it changes: it comes back as version 5.",
    content: undefined,
  });
  const fake = await several(page, [archive, unarchive]);
  const taps = await decide(fake, page, (pid) => ({
    state: "done",
    summary: pid === id(1) ? "Archived. Unarchive brings it back as it is." : "Unarchived.",
    library: { kind: "specialist", id: pid === id(1) ? "qa-runner" : "docs-editor" },
  }));
  await openAll(page);
  const cards = page.getByTestId("ask-operation");
  await expect(cards).toHaveCount(2);
  // Two cards without a specialist's content are not folded.
  await expect(page.getByTestId("ask-specialist-fold")).toHaveCount(0);
  const [a, u] = [cards.nth(0), cards.nth(1)];
  await expect(a).toContainText("Reversible");
  await expect(a.getByTestId("ask-specialist-note")).toHaveText(
    "Nothing is deleted. Unarchive brings it back as it is.",
  );
  await expect(u.getByTestId("ask-specialist-note")).toContainText(
    "Its agent, model and skills are kept as they were.",
  );
  const [archiveCta, unarchiveCta] = [
    a.getByTestId("ask-operation-confirm"),
    u.getByTestId("ask-operation-confirm"),
  ];
  await expect(archiveCta).toHaveText("Archive");
  await expect(unarchiveCta).toHaveText("Unarchive");
  // The same fill on both: archive is the shared primary, not the red of a destructive tap.
  const fill = (l: Locator) => l.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(await fill(archiveCta)).toBe(await fill(unarchiveCta));
  await minTarget(archiveCta);
  await minTarget(unarchiveCta);
  await archiveCta.click();
  await expect(a.getByTestId("ask-operation-outcome")).toContainText("Archived", {
    timeout: 20_000,
  });
  await expect(a.getByTestId("ask-library-open")).toHaveAttribute(
    "href",
    "/library/specialists/qa-runner",
  );
  await unarchiveCta.click();
  await expect(u.getByTestId("ask-operation-outcome")).toContainText("Unarchived", {
    timeout: 20_000,
  });
  expect(taps.map((t) => t.pid)).toEqual([id(1), id(2)]);
  await noSidewaysScroll(page);
});

const PLAYBOOK = {
  op: "playbook.create",
  label: "Create playbook",
  verb: "Save playbook",
  title: "Release train",
  text: "Saves a new playbook, “Release train” (1 file), to your Library.",
};
const REVIEWER_ROW = {
  title: "Code Reviewer",
  state: "exists",
  version: 4,
  engine: "claude",
  agent: "Claude Code",
  model: "claude-opus-4-5",
};
const WRITER_EXISTS = {
  title: "Release Notes Writer",
  state: "exists",
  version: 1,
  engine: "claude",
  agent: "Claude Code",
  model: "claude-sonnet-4-5",
};

/** The playbook's content: the server derives the list at every read; this fake does too, from
 *  the companion card's state. */
async function playbookContent(page: Page, fake: FakeAsk, where: "above" | "below") {
  const companion = () =>
    fake.conversations[0].turns.flatMap((t) => t.proposals ?? []).find((p) => p.action_id === id(2))!;
  await page.route(`**/api/ask/proposals/${id(1)}/content`, (r) => {
    const made = companion().state;
    return r.fulfill({
      json: {
        kind: "files",
        files: [{ path: "playbook.toml", text: 'id = "release-train"' }],
        relies: [
          made === "done"
            ? WRITER_EXISTS
            : made === "pending"
              ? { title: "Release Notes Writer", state: "proposed", where }
              : { title: "Release Notes Writer", state: "missing" },
          REVIEWER_ROW,
          { title: "QA Runner", state: "missing" },
        ],
      },
    });
  });
}

test("a playbook and the specialist it needs are two steps; its card names them and Save is never blocked (#1938, #2251 D9)", async ({
  page,
}) => {
  // Step 1 (the specialist) is done; step 2's playbook card is the only card on screen.
  const fake = await several(page, [card({ action_id: id(2), state: "done", can_confirm: false, can_dismiss: false, summary: "Created. Open it in the Library." })]);
  const conv = fake.conversations[0];
  const first = conv.turns[0];
  const second = turnOf("[BattleLab] step 1 is done", { answer: "Step 1 is done; now the playbook." });
  second.proposals = [card({ action_id: id(1), ...PLAYBOOK })];
  conv.turns.push(second);
  const step = (n: number, sid: string, title: string, state: string, cards: string[]) => ({
    sid, n, title, detail: "", needs: n === 2 ? ["s00000001"] : [], state, reason: "", outcome: "", cards, settled_at: null,
  });
  conv.step_lists = [
    {
      id: "c".repeat(16),
      revision: 2,
      title: "Release train",
      status: "active",
      current: "s00000002",
      drawn_at_turn: second.turn_id,
      created_turn: first.turn_id,
      owed: false,
      steps: [
        step(1, "s00000001", "The specialists it needs", "done", [id(2)]),
        step(2, "s00000002", "The playbook", "now", [id(1)]),
      ],
    },
  ];
  await playbookContent(page, fake, "above");
  await decide(fake, page, () => ({ state: "done", summary: "Saved." }));
  await openAll(page);
  const list = page.getByTestId("ask-steps");
  await expect(list.getByTestId("ask-steps-progress")).toContainText("Step 2 of 2");
  // ONE card on screen, in step 2's slot: the playbook.
  await expect(page.getByTestId("ask-operation")).toHaveCount(1);
  const pb = list.getByTestId("ask-step-slot").getByTestId("ask-operation");
  await expect(pb).toHaveCount(1);
  await expect(pb.getByTestId("ask-relies-counts")).toHaveText(
    "Specialists it relies on3 named2 exist0 proposed1 missing",
  );
  const rows = pb.getByTestId("ask-relies-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toHaveAttribute("data-state", "exists");
  await expect(rows.nth(0)).toContainText("Claude Code · claude-sonnet-4-5");
  await expect(rows.nth(1)).toContainText("version 4");
  await expect(rows.nth(2)).toContainText("Missing");
  await expect(rows.nth(2)).toContainText("No specialist has this job yet, and none is proposed.");
  // The list sits above the files, and the note says what saving does not do.
  const listY = (await pb.getByTestId("ask-relies").boundingBox())!.y;
  const filesY = (await pb.getByTestId("ask-library-file").boundingBox())!.y;
  expect(listY).toBeLessThan(filesY);
  await expect(pb.getByTestId("ask-relies-note")).toContainText(
    "Saving the playbook creates no specialist.",
  );
  // A missing specialist: Save playbook is enabled all the same.
  const save = pb.getByTestId("ask-operation-confirm");
  await expect(save).toHaveText("Save playbook");
  await expect(save).toBeEnabled();
  await minTarget(save);
  await noSidewaysScroll(page);
});

test("cards of separate requests: the playbook's row follows its companion's tap, in either order (#1938)", async ({
  page,
}) => {
  // Two requests, one kind of card each: the specialist asked for first, the playbook after.
  const fake = await several(page, [card({ action_id: id(2) })]);
  const second = turnOf("and a release-train playbook", { answer: "Proposed." });
  second.proposals = [card({ action_id: id(1), ...PLAYBOOK })];
  fake.conversations[0].turns.push(second);
  await playbookContent(page, fake, "above");
  await content(page, id(2), CREATE);
  await decide(fake, page, () => ({ state: "done", summary: "Created. Open it in the Library." }));
  await openAll(page);
  const cards = page.getByTestId("ask-operation");
  const pb = cards.nth(1);
  const rows = pb.getByTestId("ask-relies-row");
  await expect(pb.getByTestId("ask-relies-counts")).toHaveText(
    "Specialists it relies on3 named1 exists1 proposed1 missing",
  );
  await expect(rows.nth(0)).toHaveAttribute("data-state", "proposed");
  await expect(rows.nth(0)).toContainText("Proposed above");
  await expect(rows.nth(0)).toContainText("Its card is above in this conversation. Not created yet.");
  const save = pb.getByTestId("ask-operation-confirm");
  await expect(save).toBeEnabled();
  // Confirming the companion first: the playbook card's row follows, with no reload.
  await cards.nth(0).getByTestId("ask-operation-confirm").click();
  await expect(rows.nth(0)).toHaveAttribute("data-state", "exists", { timeout: 20_000 });
  await expect(pb.getByTestId("ask-relies-counts")).toContainText("2 exist");
  await expect(save).toBeEnabled();
  await noSidewaysScroll(page);
});

test("an out-of-date card keeps its values, marks the pin that moved and asks HQ again (#1938)", async ({
  page,
}) => {
  await shell(
    page,
    card({
      state: "stale",
      reason: "Skill changelog-style changed since you loaded it: reload and save again. Nothing was created.",
      can_confirm: false,
      can_dismiss: false,
    }),
  );
  await content(page, id(1), {
    ...CREATE,
    skills_now: { ...NOW, "changelog-style": "sha256:99990000" },
  });
  let sent: Record<string, unknown> | null = null;
  await page.route(ASK_STREAM, async (r) => {
    sent = r.request().postDataJSON() as Record<string, unknown>;
    await fulfillAsk(r, { answer: "Proposed again." });
  });
  const c = await open(page);
  await expect(c).toHaveAttribute("data-state", "stale");
  await expect(c.getByTestId("ask-specialist-staged")).toContainText("As proposed.");
  await expect(c.getByTestId("ask-specialist-row")).toHaveCount(7);
  const moved = c.locator('[data-testid="ask-specialist-skill"][data-moved="true"]');
  await expect(moved).toHaveText("!changelog-style · rev aaaa1111 · now rev 99990000");
  const out = c.getByTestId("ask-operation-outcome");
  await expect(out).toContainText("Out of date");
  await expect(out).toContainText("Nothing was created.");
  await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
  const again = c.getByTestId("ask-operation-again");
  await expect(again).toHaveText("Ask HQ again");
  await minTarget(again);
  if ((page.viewportSize()?.width ?? 0) <= 800) {
    // Full width on a phone.
    const [row, btn] = [(await out.boundingBox())!, (await again.boundingBox())!];
    expect(btn.width).toBeGreaterThan(row.width - 40);
  }
  await noSidewaysScroll(page);
  await again.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("ask-turn")).toHaveCount(2);
  expect(sent).toMatchObject({
    text: "The card “New specialist: Release Notes Writer” is out of date. Propose it again as it stands now.",
  });
});

test("a store refusal reads Refused, in the warning tone, and offers nothing (#1938)", async ({
  page,
}) => {
  await shell(
    page,
    card({
      op: "specialist.archive",
      label: "Archive specialist",
      verb: "Archive",
      title: "Playbook Writer · version 1",
      text: "No new launches. Versions that missions pinned stay readable.",
      content: undefined,
      state: "refused",
      summary: "Built-in staff can't be archived. Nothing changed.",
      can_confirm: false,
      can_dismiss: false,
    }),
  );
  const c = await open(page);
  const out = c.getByTestId("ask-operation-outcome");
  await expect(out).toContainText("Refused");
  await expect(out).toContainText("Built-in staff can't be archived. Nothing changed.");
  await expect(c.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await expect(c.getByTestId("ask-operation-again")).toHaveCount(0);
  await expect(c.getByTestId("ask-specialist-note")).toHaveCount(0);
  // The warning tone (the stale card's), not the failure's red.
  const word = out.getByText("Refused", { exact: true });
  const tone = await word.evaluate((el) => getComputedStyle(el).color);
  const probe = await page.evaluate(() => {
    const el = document.createElement("span");
    document.body.append(el);
    const read = (v: string) => {
      el.style.color = `var(${v})`;
      return getComputedStyle(el).color;
    };
    const out = { warn: read("--warn-text"), danger: read("--danger-text") };
    el.remove();
    return out;
  });
  expect(tone).toBe(probe.warn);
  expect(tone).not.toBe(probe.danger);
});

test("six specialist cards fold to rows: no CTA until one is opened, then only its own (#1938)", async ({
  page,
}) => {
  const names = [
    "Release Notes Writer",
    "Code Reviewer",
    "QA Runner",
    "Docs Editor",
    "Security Auditor",
    "Migration Planner",
  ];
  const cards = names.map((title, i) =>
    card({ action_id: id(i + 1), title, text: `Creates the specialist ${title} in your Library.` }),
  );
  const fake = await several(page, cards);
  for (const [i, title] of names.entries())
    await content(page, id(i + 1), { ...CREATE, after: { ...WRITER, title, capacity: i + 1 } });
  const taps = await decide(fake, page, () => ({
    state: "done",
    summary: "Created. Open it in the Library.",
  }));
  await openAll(page);
  const all = page.getByTestId("ask-operation");
  await expect(all).toHaveCount(6);
  await expect(page.getByTestId("ask-specialist-tally")).toHaveText(
    "Proposals · Specialist · 6 waiting · 0 decided",
  );
  const folds = page.getByTestId("ask-specialist-fold");
  await expect(folds).toHaveCount(6);
  // Folded: one full-width button each, at the floor, and no Confirm anywhere.
  await expect(page.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await expect(page.getByTestId("ask-operation-dismiss")).toHaveCount(0);
  for (let i = 0; i < 6; i++) {
    await minTarget(folds.nth(i));
    await expect(folds.nth(i)).toHaveAttribute("aria-expanded", "false");
    await expect(folds.nth(i)).toContainText(names[i]);
    await expect(folds.nth(i)).toContainText("Review");
  }
  await expect(folds.nth(1)).toContainText("02");
  await expect(folds.nth(1).getByTestId("ask-specialist-fold-meta")).toHaveText(
    "Claude Code · claude-sonnet-4-5",
  );
  await expect(folds.nth(1)).toContainText("Up to 2 at once");
  const [rowBox, cardBox] = [(await folds.nth(0).boundingBox())!, (await all.nth(0).boundingBox())!];
  expect(rowBox.width).toBeGreaterThan(cardBox.width - 8);
  await noSidewaysScroll(page);

  // Keyboard: Tab walks the rows in order; Enter opens one; its content, then its CTA, follow.
  await folds.nth(0).focus();
  await page.keyboard.press("Tab");
  await expect(folds.nth(1)).toBeFocused();
  expect(await folds.nth(1).evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(folds.nth(1)).toHaveAttribute("aria-expanded", "true");
  await expect(folds.nth(1)).toContainText("Fold");
  const opened = all.nth(1);
  await expect(opened.getByTestId("ask-specialist-row")).toHaveCount(7);
  const cta = opened.getByTestId("ask-operation-confirm");
  await expect(cta).toBeEnabled();
  await expect(page.getByTestId("ask-operation-confirm")).toHaveCount(1);
  await page.keyboard.press("Tab");
  await expect(cta).toBeFocused();
  await minTarget(cta);
  await page.keyboard.press("Enter");
  await expect(opened.getByTestId("ask-operation-outcome")).toContainText("Done", {
    timeout: 20_000,
  });
  expect(taps).toEqual([{ pid: id(2), decision: "confirm" }]);
  await expect(page.getByTestId("ask-specialist-tally")).toHaveText(
    "Proposals · Specialist · 5 waiting · 1 decided",
  );
  // Folded again, a decided card shows its outcome word where Review was.
  await folds.nth(1).click();
  await expect(folds.nth(1)).toHaveAttribute("aria-expanded", "false");
  await expect(folds.nth(1).getByTestId("ask-specialist-fold-go")).toHaveText("Done");
  await expect(page.getByTestId("ask-operation-confirm")).toHaveCount(0);
});

test("six specialist cards in a step's slot fold the same way, the tally once (#1938, #2251)", async ({
  page,
}) => {
  const cards = Array.from({ length: 6 }, (_, i) =>
    card({ action_id: id(i + 1), title: `Specialist ${i + 1}` }),
  );
  const fake = await several(page, cards);
  const conv = fake.conversations[0];
  const turn = conv.turns[0].turn_id;
  conv.step_lists = [
    {
      id: "c".repeat(16),
      revision: 1,
      title: "Release train",
      status: "active",
      current: "s00000001",
      drawn_at_turn: turn,
      created_turn: turn,
      owed: false,
      steps: [
        { sid: "s00000001", n: 1, title: "The specialists it needs", detail: "", needs: [], state: "now", reason: "", outcome: "", cards: cards.map((c) => c.action_id), settled_at: null },
        { sid: "s00000002", n: 2, title: "The playbook", detail: "", needs: ["s00000001"], state: "next", reason: "", outcome: "", cards: [], settled_at: null },
      ],
    },
  ];
  for (let i = 0; i < 6; i++) await content(page, id(i + 1), CREATE);
  await openAll(page);
  const slot = page.getByTestId("ask-steps").getByTestId("ask-step-slot");
  // Drawn once, in the slot; folded, with one tally and no Confirm until a row is opened.
  await expect(page.getByTestId("ask-operation")).toHaveCount(6);
  await expect(slot.getByTestId("ask-specialist-fold")).toHaveCount(6);
  await expect(page.getByTestId("ask-specialist-tally")).toHaveCount(1);
  await expect(slot.getByTestId("ask-specialist-tally")).toHaveText(
    "Proposals · Specialist · 6 waiting · 0 decided",
  );
  await expect(page.getByTestId("ask-operation-confirm")).toHaveCount(0);
  await slot.getByTestId("ask-specialist-fold").nth(2).click();
  await expect(page.getByTestId("ask-operation-confirm")).toHaveCount(1);
  await noSidewaysScroll(page);
});

test("one specialist card alone is not folded (#1938)", async ({ page }) => {
  await shell(page, card());
  await content(page, id(1), CREATE);
  const c = await open(page);
  await expect(c.getByTestId("ask-specialist-row")).toHaveCount(7);
  await expect(page.getByTestId("ask-specialist-fold")).toHaveCount(0);
  await expect(page.getByTestId("ask-specialist-tally")).toHaveCount(0);
});
