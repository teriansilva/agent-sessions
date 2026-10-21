/** The objective list's operator half (#889), and the four meanings of an empty one.
 *
 *  The rule under test throughout: **an edit is never a claim that an objective holds.** The
 *  route refuses `state` / `met_at` / `observed`, so the only settlement an operator can write is
 *  `waived` — a decision that the objective was not required. The control is labelled for that
 *  meaning, and a test asserts the label, because "WAIVE" next to a green dot is exactly how an
 *  operator comes to believe something was verified.
 *
 *  Since #967 P3 every row action is in the row's ⋯ menu and keeps its testid, so these open the
 *  menu first — as the operator does. The drag itself is proven in a real browser
 *  (`e2e/mission-objectives-dnd.spec.ts`); what is pinned here is the order it sends and what the
 *  list shows while the server has not answered, or has refused.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type {
  MissionObjective,
  MissionSupervisor,
  SupervisorObjective,
} from "../../types/api";

import { MissionObjectives } from "./MissionObjectives";

function obj(over: Partial<MissionObjective> = {}): MissionObjective {
  return {
    key: "pr_open",
    title: "A PR is open",
    gate: true,
    state: "pending",
    met_at: null,
    observed: null,
    source: "playbook",
    ...over,
  } as MissionObjective;
}

/** Open row `i`'s ⋯ menu. */
async function openMenu(i = 0) {
  await userEvent.click(screen.getAllByTestId("objective-menu")[i]);
  return screen.getByRole("menu");
}

function order(): (string | null)[] {
  return screen
    .getAllByTestId("objective")
    .map((li) => li.getAttribute("data-key"));
}

test("an empty list says WHICH kind of empty it is", () => {
  const { rerender } = render(
    <MissionObjectives objectives={[]} objectivesState="pending" />,
  );
  // The producer has not answered yet. Rendering this as "no objectives" states a fact the
  // server has not established — the same family of lie as a stale probe reported as current.
  expect(screen.getByTestId("objectives-pending")).toBeInTheDocument();
  expect(screen.queryByTestId("objectives-empty")).toBeNull();

  rerender(<MissionObjectives objectives={[]} objectivesState="failed" />);
  expect(screen.getByTestId("objectives-unavailable")).toHaveTextContent(
    /could not be produced/i,
  );

  rerender(<MissionObjectives objectives={[]} objectivesState="skipped" />);
  expect(screen.getByTestId("objectives-unavailable")).toHaveTextContent(
    /none were proposed|No objectives were proposed/i,
  );

  rerender(<MissionObjectives objectives={[]} objectivesState={null} />);
  expect(screen.getByTestId("objectives-empty")).toBeInTheDocument();
});

test("the settle control says NOT REQUIRED, never 'met'", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
  await openMenu();
  const waive = screen.getByTestId("objective-waive");
  expect(waive).toHaveTextContent(/NOT REQUIRED/i);
  expect(waive).not.toHaveTextContent(/\bmet\b/i);
  expect(waive.getAttribute("aria-label")).toMatch(/not required/i);
});

test("edits post as ONE batch of ops, matching the route's single transaction", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
  await openMenu();
  await userEvent.click(screen.getByTestId("objective-waive"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  // An ARRAY, not a call per row. The route applies ops in one transaction under the write
  // fence; a request per op would let a multi-op edit half-apply.
  expect(onOps.mock.calls[0][0]).toEqual([{ op: "waive", key: "pr_open" }]);
});

test("an already-settled objective offers no settle control", async () => {
  render(
    <MissionObjectives
      objectives={[obj({ state: "met", met_at: 100 })]}
      onOps={vi.fn()}
    />,
  );
  await openMenu();
  // Waiving something already observed to hold would replace a verified fact with a weaker
  // claim. REMOVE stays: dropping an objective is a different decision.
  expect(screen.getByTestId("objective-waive")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(screen.getByTestId("objective-drop")).not.toHaveAttribute(
    "aria-disabled",
  );
});

test("without `onOps` the list is READ-ONLY — no control appears at all", () => {
  render(<MissionObjectives objectives={[obj()]} />);
  expect(screen.queryByTestId("objective-menu")).toBeNull();
  expect(screen.queryByTestId("objective-handle")).toBeNull();
  expect(screen.queryByTestId("objective-waive")).toBeNull();
  expect(screen.queryByTestId("objective-drop")).toBeNull();
  expect(screen.queryByTestId("objective-add")).toBeNull();
  // …and the row itself still renders. Read-only is not "hidden".
  expect(screen.getByTestId("objective")).toBeInTheDocument();
});

test("adding an objective sends `add` with a key the store can arbitrate", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  render(<MissionObjectives objectives={[]} onOps={onOps} />);
  await userEvent.type(
    screen.getByTestId("objective-add-input"),
    "The deploy is verified",
  );
  await userEvent.click(screen.getByTestId("objective-add"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  const ops = onOps.mock.calls[0][0];
  expect(ops).toHaveLength(1);
  expect(ops[0].op).toBe("add");
  expect(ops[0].title).toBe("The deploy is verified");
  expect(typeof ops[0].key).toBe("string");
  // No `state`, no `met_at`, no `observed` — the client must not even attempt to author a
  // settlement, quite apart from the route refusing it.
  expect(Object.keys(ops[0]).sort()).toEqual(["key", "op", "title"]);
});

test("a stale probe still renders its last observed state and the reason", () => {
  render(
    <MissionObjectives
      objectives={[
        obj({
          state: "pending",
          observed: {
            stale: true,
            at: 1_700_000_000,
            reason: "forge unreachable",
          },
        }),
      ]}
    />,
  );
  const row = screen.getByTestId("objective");
  expect(within(row).getByTestId("objective-stale")).toHaveTextContent(
    /stale/i,
  );
  expect(row).toHaveTextContent("forge unreachable");
  // Nothing was marked met on data the server could not fetch.
  expect(row).toHaveTextContent("pending");
});

// ---- the ops the route accepts but nothing rendered (#896 review, finding 1) -----------------

test("RENAME posts a `retitle` op carrying the new title", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
  await openMenu();
  await userEvent.click(screen.getByTestId("objective-rename"));
  const box = screen.getByTestId("objective-rename-input");
  // Seeded with the current title: a rename control that starts empty is a delete-and-retype.
  expect(box).toHaveValue("A PR is open");
  await userEvent.clear(box);
  await userEvent.type(box, "A PR is open against main");
  await userEvent.click(screen.getByTestId("objective-rename-save"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  expect(onOps.mock.calls[0][0]).toEqual([
    { op: "retitle", key: "pr_open", title: "A PR is open against main" },
  ]);
});

test("REORDER posts the WHOLE key list in its new order, as one op", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  const rows = [
    obj({ key: "a", title: "A" }),
    obj({ key: "b", title: "B" }),
    obj({ key: "c", title: "C" }),
  ];
  render(<MissionObjectives objectives={rows} onOps={onOps} />);
  // Move B up, from its menu — the keyboard-and-tap path that never depends on dragging.
  await openMenu(1);
  await userEvent.click(screen.getByTestId("objective-up"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  // The route replaces the order wholesale, so the move is computed here and sent once — a pair
  // of swaps could half-apply and leave an order nobody chose.
  expect(onOps.mock.calls[0][0]).toEqual([
    { op: "reorder", keys: ["b", "a", "c"] },
  ]);
});

test("the ends cannot be moved off the list", async () => {
  const rows = [obj({ key: "a" }), obj({ key: "b" })];
  render(<MissionObjectives objectives={rows} onOps={vi.fn()} />);
  // Disabled rather than wrapping: a control that silently sends a row to the far end is worse
  // than one that says it cannot move.
  await openMenu(0);
  expect(screen.getByTestId("objective-up")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await userEvent.keyboard("{Escape}");
  await openMenu(1);
  expect(screen.getByTestId("objective-down")).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(screen.getByTestId("objective-up")).not.toHaveAttribute(
    "aria-disabled",
  );
});

test("read-only hides the reorder and rename controls too", () => {
  render(<MissionObjectives objectives={[obj()]} />);
  expect(screen.queryByTestId("objective-up")).toBeNull();
  expect(screen.queryByTestId("objective-rename")).toBeNull();
  expect(screen.queryByTestId("objective-handle")).toBeNull();
});

// ---- #967 P3: the order the list shows while the server decides -------------------------------

function deferred() {
  let resolve!: (ok: boolean) => void;
  const promise = new Promise<boolean>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const ABC = [
  obj({ key: "a", title: "A" }),
  obj({ key: "b", title: "B" }),
  obj({ key: "c", title: "C" }),
];

test("a REFUSED reorder puts the server's order back, and says it did not apply", async () => {
  const answer = deferred();
  const onOps = vi.fn().mockReturnValue(answer.promise);
  render(<MissionObjectives objectives={ABC} onOps={onOps} />);

  await openMenu(2);
  await userEvent.click(screen.getByTestId("objective-up"));
  // Shown at once, before the server answers…
  expect(order()).toEqual(["a", "c", "b"]);
  expect(screen.queryByTestId("objectives-reorder-refused")).toBeNull();

  // …and dropped when it refuses (a 409 because the list changed, or a 422).
  await act(async () => answer.resolve(false));
  expect(order()).toEqual(["a", "b", "c"]);
  expect(screen.getByTestId("objectives-reorder-refused")).toHaveTextContent(
    /did not apply/i,
  );
});

test("an ACCEPTED reorder is shown until the list is re-read, then the re-read wins", async () => {
  const onOps = vi.fn().mockResolvedValue(true);
  const { rerender } = render(
    <MissionObjectives objectives={ABC} onOps={onOps} />,
  );
  await openMenu(2);
  await userEvent.click(screen.getByTestId("objective-up"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  // Accepted, not yet re-read: the operator's order stays rather than flicking back.
  expect(order()).toEqual(["a", "c", "b"]);

  // The re-read is the answer, whatever it says — here someone else's order landed first.
  rerender(
    <MissionObjectives
      objectives={[ABC[1], ABC[0], ABC[2]]}
      onOps={onOps}
    />,
  );
  expect(order()).toEqual(["b", "a", "c"]);
  expect(screen.queryByTestId("objectives-reorder-refused")).toBeNull();
});

function reading(over: Partial<SupervisorObjective>): SupervisorObjective {
  return {
    key: "a",
    title: "A",
    gate: false,
    state: "pending",
    met: false,
    episode: 2,
    stood_down: false,
    awaiting_answer: false,
    spent: 0,
    remaining: 3,
    may_nudge: false,
    unreadable: false,
    indeterminate: false,
    live: 0,
    terminal: false,
    why_not: "",
    ...over,
  };
}

const NO_SESSION = "this mission holds no session, so there is nothing to nudge";

test("with no session the reason is said ONCE, and Stand down is disabled with its reason", async () => {
  const supervisor: MissionSupervisor = {
    objectives: ABC.map((o) =>
      reading({ key: o.key, title: o.title, why_not: NO_SESSION }),
    ),
    likely_done: false,
    unmet_gates: 0,
    checked_at: 1,
    held_sessions: 0,
    no_session: true,
  };
  const onStandDown = vi.fn();
  render(
    <MissionObjectives
      objectives={ABC}
      supervisor={supervisor}
      showNotices={false}
      onOps={vi.fn().mockResolvedValue(true)}
      onStandDown={onStandDown}
    />,
  );
  expect(screen.getAllByText(NO_SESSION)).toHaveLength(1);
  expect(screen.getByTestId("objectives-shared-reason")).toHaveTextContent(
    NO_SESSION,
  );

  await openMenu(0);
  const stand = screen.getByTestId("objective-stand-down");
  expect(stand).toHaveAttribute("aria-disabled", "true");
  expect(stand).toHaveTextContent("No session to nudge");
  await userEvent.click(stand);
  expect(onStandDown).not.toHaveBeenCalled();
});

test("a row whose reason differs keeps its own sentence", () => {
  const SPENT = "the 3-nudge budget for this episode is spent";
  const supervisor: MissionSupervisor = {
    objectives: [
      reading({ key: "a", why_not: SPENT }),
      reading({ key: "b", why_not: NO_SESSION }),
      reading({ key: "c", why_not: NO_SESSION }),
    ],
    likely_done: false,
    unmet_gates: 0,
    checked_at: 1,
    no_session: true,
  };
  render(<MissionObjectives objectives={ABC} supervisor={supervisor} />);
  expect(screen.getAllByText(NO_SESSION)).toHaveLength(1);
  const rows = screen.getAllByTestId("objective");
  expect(within(rows[0]).getByText(SPENT)).toBeInTheDocument();
  expect(within(rows[1]).queryByText(NO_SESSION)).toBeNull();
});

test("the drag handle is a named control the keyboard can reach", () => {
  render(
    <MissionObjectives objectives={ABC} onOps={vi.fn().mockResolvedValue(true)} />,
  );
  const handles = screen.getAllByTestId("objective-handle");
  expect(handles).toHaveLength(3);
  expect(handles[0]).toHaveAccessibleName('Reorder "A"');
  expect(handles[0]).toHaveAttribute("aria-roledescription", "sortable");
  expect(handles[0].tabIndex).toBe(0);
});

// ---- #967 P3, Hermes on #985: the full title stays reachable -----------------------------------

const LONG =
  "Every consumer of the billing API has moved to the new client, and the old service is retired only after the migration report is signed off";

/** jsdom has no layout: give the title text a clamped height and a content height by length, and an
 *  observer that reports once as it starts, as a browser's does. */
function stubTitleLayout(): () => void {
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  Object.defineProperty(proto, "clientHeight", {
    configurable: true,
    get() {
      return 35;
    },
  });
  Object.defineProperty(proto, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return (this.textContent ?? "").length > 60 ? 88 : 35;
    },
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      private cb: () => void;
      constructor(cb: () => void) {
        this.cb = cb;
      }
      observe() {
        this.cb();
      }
      unobserve() {}
      disconnect() {}
    },
  );
  return () => {
    delete proto.clientHeight;
    delete proto.scrollHeight;
    vi.unstubAllGlobals();
  };
}

test("a clipped title opens and closes by click and keyboard; a title that fits stays text", async () => {
  const restore = stubTitleLayout();
  try {
    // READ-ONLY — no `onOps`, no ⋯ — which is exactly the row that had no other way to the text.
    render(
      <MissionObjectives
        objectives={[
          obj({ key: "long", title: LONG }),
          obj({ key: "short", title: "Docs updated" }),
        ]}
      />,
    );
    const rows = screen.getAllByTestId("objective");
    expect(within(rows[0]).queryByTestId("objective-menu")).toBeNull();
    const toggle = await within(rows[0]).findByTestId("objective-title-toggle");
    expect(toggle).toHaveAttribute("role", "button");
    expect(toggle.tabIndex).toBe(0);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAccessibleName(LONG);
    expect(toggle).toHaveTextContent(/more$/);

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(rows[0]).getByTestId("objective-title").className).toMatch(
      /objTitleOpen/,
    );
    expect(toggle).toHaveTextContent(/less$/);

    toggle.focus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    // The short title gained nothing.
    expect(within(rows[1]).queryByTestId("objective-title-toggle")).toBeNull();
    expect(within(rows[1]).queryByRole("button")).toBeNull();
  } finally {
    restore();
  }
});

test("opening a title on an editable row sends nothing", async () => {
  const restore = stubTitleLayout();
  try {
    const onOps = vi.fn().mockResolvedValue(true);
    render(
      <MissionObjectives
        objectives={[obj({ key: "long", title: LONG }), obj({ key: "b", title: "B" })]}
        onOps={onOps}
      />,
    );
    await userEvent.click(await screen.findByTestId("objective-title-toggle"));
    expect(screen.getByTestId("objective-title-toggle")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(onOps).not.toHaveBeenCalled();
    expect(order()).toEqual(["long", "b"]);
  } finally {
    restore();
  }
});
