/** A specialist card on the operation card (#1938): the group word, the Refused outcome, the
 *  fold, the staged note and "Ask HQ again". Layout, targets and keyboard order are the
 *  real-browser spec's (`e2e/ask-specialists.spec.ts`). */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { AskLibraryContent, AskOperationCard } from "../../types/api";
import { OperationCard } from "./AskCards";

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
  api: { askProposalContent: vi.fn() },
}));

const PID = "a".repeat(32);
const WRITER = {
  title: "Release Notes Writer",
  summary: "Writes the release notes.",
  engine: "claude",
  agent: "Claude Code",
  model: "claude-sonnet-4-5",
  effort: null,
  grade: null,
  skills: [{ id: "changelog-style", revision: "sha256:aaaa1111" }],
  capacity: 3,
};
const CREATE: AskLibraryContent = {
  kind: "specialist",
  action: "create",
  after: WRITER,
  before: null,
  skills_now: { "changelog-style": "sha256:aaaa1111" },
};

function card(over: Partial<AskOperationCard> = {}): AskOperationCard {
  return {
    action_id: PID,
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

function mount(p: AskOperationCard, props: Partial<Parameters<typeof OperationCard>[0]> = {}) {
  const onConfirm = vi.fn();
  render(
    <MemoryRouter>
      <OperationCard
        p={p}
        deciding={false}
        error={null}
        onConfirm={onConfirm}
        onDismiss={vi.fn()}
        {...props}
      />
    </MemoryRouter>,
  );
  return { onConfirm };
}

beforeEach(() => {
  vi.mocked(api.askProposalContent).mockReset();
  vi.mocked(api.askProposalContent).mockResolvedValue(CREATE);
});

test("a new specialist offers Create only once its seven rows are on screen", async () => {
  let land: (c: AskLibraryContent) => void = () => {};
  vi.mocked(api.askProposalContent).mockReturnValue(new Promise((r) => (land = r)));
  const { onConfirm } = mount(card());
  const c = screen.getByTestId("ask-operation");
  expect(c).toHaveTextContent("Specialist");
  expect(c).toHaveTextContent("Reversible");
  const cta = within(c).getByTestId("ask-operation-confirm");
  expect(cta).toHaveTextContent("Create specialist");
  expect(cta).toBeDisabled();
  land(CREATE);
  expect(await within(c).findAllByTestId("ask-specialist-row")).toHaveLength(7);
  expect(within(c).getByTestId("ask-specialist-grade")).toHaveTextContent("Ungraded");
  expect(cta).toBeEnabled();
  await userEvent.click(cta);
  expect(onConfirm).toHaveBeenCalledWith(undefined);
});

test("a folded card has no Confirm until it is opened, and its row names what it runs on", async () => {
  mount(card(), { fold: 2 });
  const c = screen.getByTestId("ask-operation");
  const row = within(c).getByTestId("ask-specialist-fold");
  expect(c).toHaveAttribute("data-folded", "true");
  expect(row).toHaveAttribute("aria-expanded", "false");
  expect(row).toHaveTextContent("02");
  expect(row).toHaveTextContent("Review");
  expect(await within(row).findByText("Claude Code · claude-sonnet-4-5")).toBeInTheDocument();
  expect(row).toHaveTextContent("Up to 3 at once");
  expect(within(c).queryByTestId("ask-operation-confirm")).toBeNull();
  expect(within(c).queryByTestId("ask-specialist-row")).toBeNull();

  await userEvent.click(row);
  expect(row).toHaveAttribute("aria-expanded", "true");
  expect(row).toHaveTextContent("Fold");
  expect(await within(c).findAllByTestId("ask-specialist-row")).toHaveLength(7);
  expect(within(c).getByTestId("ask-operation-confirm")).toBeEnabled();

  await userEvent.click(row);
  expect(within(c).queryByTestId("ask-operation-confirm")).toBeNull();
});

test("a decided folded card shows its outcome word in place of Review", () => {
  mount(card({ state: "done", summary: "Created.", can_confirm: false, can_dismiss: false }), {
    fold: 1,
  });
  const go = screen.getByTestId("ask-specialist-fold-go");
  expect(go).toHaveTextContent("Done");
  expect(go).not.toHaveTextContent("Review");
});

test("a store refusal reads Refused with the store's sentence, and offers nothing", () => {
  mount(
    card({
      op: "specialist.archive",
      label: "Archive specialist",
      verb: "Archive",
      content: undefined,
      state: "refused",
      summary: "Built-in staff can't be archived. Nothing changed.",
      can_confirm: false,
      can_dismiss: false,
    }),
    { onAskAgain: vi.fn() },
  );
  const out = screen.getByTestId("ask-operation-outcome");
  expect(out).toHaveTextContent("Refused");
  expect(out).toHaveTextContent("Built-in staff can't be archived. Nothing changed.");
  expect(screen.queryByTestId("ask-operation-confirm")).toBeNull();
  expect(screen.queryByTestId("ask-operation-again")).toBeNull();
});

test("an archive card says nothing is deleted while it waits, with the shared primary CTA", () => {
  mount(card({ op: "specialist.archive", label: "Archive specialist", verb: "Archive", content: undefined }));
  expect(screen.getByTestId("ask-specialist-note")).toHaveTextContent(
    "Nothing is deleted. Unarchive brings it back as it is.",
  );
  const cta = screen.getByTestId("ask-operation-confirm");
  expect(cta).toBeEnabled();
  expect(cta.className).toContain("cta");
  expect(cta.className).not.toContain("ctaDanger");
});

test("an out-of-date card keeps its values under the staged note and asks HQ again", async () => {
  vi.mocked(api.askProposalContent).mockResolvedValue({
    ...CREATE,
    skills_now: { "changelog-style": "sha256:bbbb2222" },
  } as AskLibraryContent);
  const onAskAgain = vi.fn();
  mount(
    card({
      state: "stale",
      reason: "Skill changelog-style changed since you loaded it. Nothing was created.",
      can_confirm: false,
      can_dismiss: false,
    }),
    { onAskAgain },
  );
  expect(await screen.findByTestId("ask-specialist-staged")).toHaveTextContent("As proposed.");
  expect(screen.getAllByTestId("ask-specialist-row")).toHaveLength(7);
  const moved = screen.getByTestId("ask-specialist-skill");
  expect(moved).toHaveAttribute("data-moved", "true");
  expect(moved).toHaveTextContent("!changelog-style · rev aaaa1111 · now rev bbbb2222");
  expect(screen.getByTestId("ask-operation-outcome")).toHaveTextContent("Out of date");
  await userEvent.click(screen.getByTestId("ask-operation-again"));
  expect(onAskAgain).toHaveBeenCalledTimes(1);
});

test("a playbook card's list of specialists is read again when a companion card changes", async () => {
  const relies = (state: "proposed" | "missing") => ({
    kind: "files" as const,
    files: [{ path: "playbook.toml", text: "id = 1" }],
    relies: [
      state === "proposed"
        ? { title: "Release Notes Writer", state, where: "below" as const }
        : { title: "Release Notes Writer", state },
    ],
  });
  vi.mocked(api.askProposalContent).mockResolvedValue(relies("proposed"));
  const p = card({ op: "playbook.create", label: "Create playbook", verb: "Save playbook" });
  const props = { deciding: false, error: null, onConfirm: vi.fn(), onDismiss: vi.fn() };
  const { rerender } = render(
    <MemoryRouter>
      <OperationCard p={p} {...props} watch="b:pending" />
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("ask-relies-row")).toHaveAttribute("data-state", "proposed");
  expect(screen.getByTestId("ask-operation-confirm")).toBeEnabled();
  vi.mocked(api.askProposalContent).mockResolvedValue(relies("missing"));
  rerender(
    <MemoryRouter>
      <OperationCard p={p} {...props} watch="b:dismissed" />
    </MemoryRouter>,
  );
  await vi.waitFor(() =>
    expect(screen.getByTestId("ask-relies-row")).toHaveAttribute("data-state", "missing"),
  );
  // A missing specialist never disables Save, and the note says what saving does not do.
  expect(screen.getByTestId("ask-operation-confirm")).toBeEnabled();
  expect(screen.getByTestId("ask-relies-note")).toHaveTextContent(
    "Saving the playbook creates no specialist.",
  );
});
