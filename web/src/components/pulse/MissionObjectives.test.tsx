/** The objective list's operator half (#889), and the four meanings of an empty one.
 *
 *  The rule under test throughout: **an edit is never a claim that an objective holds.** The
 *  route refuses `state` / `met_at` / `observed`, so the only settlement an operator can write is
 *  `waived` — a decision that the objective was not required. The control is labelled for that
 *  meaning, and a test asserts the label, because "WAIVE" next to a green dot is exactly how an
 *  operator comes to believe something was verified.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { MissionObjective } from "../../types/api";

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
  const onOps = vi.fn().mockResolvedValue(undefined);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
  const waive = screen.getByTestId("objective-waive");
  expect(waive).toHaveTextContent(/NOT REQUIRED/i);
  expect(waive).not.toHaveTextContent(/\bmet\b/i);
  expect(waive.getAttribute("aria-label")).toMatch(/not required/i);
});

test("edits post as ONE batch of ops, matching the route's single transaction", async () => {
  const onOps = vi.fn().mockResolvedValue(undefined);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
  await userEvent.click(screen.getByTestId("objective-waive"));
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  // An ARRAY, not a call per row. The route applies ops in one transaction under the write
  // fence; a request per op would let a multi-op edit half-apply.
  expect(onOps.mock.calls[0][0]).toEqual([{ op: "waive", key: "pr_open" }]);
});

test("an already-settled objective offers no settle control", () => {
  render(
    <MissionObjectives
      objectives={[obj({ state: "met", met_at: 100 })]}
      onOps={vi.fn()}
    />,
  );
  // Waiving something already observed to hold would replace a verified fact with a weaker
  // claim. REMOVE stays: dropping an objective is a different decision.
  expect(screen.getByTestId("objective-waive")).toBeDisabled();
  expect(screen.getByTestId("objective-drop")).toBeEnabled();
});

test("without `onOps` the list is READ-ONLY — no control appears at all", () => {
  render(<MissionObjectives objectives={[obj()]} />);
  expect(screen.queryByTestId("objective-waive")).toBeNull();
  expect(screen.queryByTestId("objective-drop")).toBeNull();
  expect(screen.queryByTestId("objective-add")).toBeNull();
  // …and the row itself still renders. Read-only is not "hidden".
  expect(screen.getByTestId("objective")).toBeInTheDocument();
});

test("adding an objective sends `add` with a key the store can arbitrate", async () => {
  const onOps = vi.fn().mockResolvedValue(undefined);
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
  const onOps = vi.fn().mockResolvedValue(undefined);
  render(<MissionObjectives objectives={[obj()]} onOps={onOps} />);
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
  const onOps = vi.fn().mockResolvedValue(undefined);
  const rows = [
    obj({ key: "a", title: "A" }),
    obj({ key: "b", title: "B" }),
    obj({ key: "c", title: "C" }),
  ];
  render(<MissionObjectives objectives={rows} onOps={onOps} />);
  // Move B up.
  await userEvent.click(screen.getAllByTestId("objective-up")[1]);
  await waitFor(() => expect(onOps).toHaveBeenCalled());
  // The route replaces the order wholesale, so the move is computed here and sent once — a pair
  // of swaps could half-apply and leave an order nobody chose.
  expect(onOps.mock.calls[0][0]).toEqual([
    { op: "reorder", keys: ["b", "a", "c"] },
  ]);
});

test("the ends cannot be moved off the list", () => {
  const rows = [obj({ key: "a" }), obj({ key: "b" })];
  render(<MissionObjectives objectives={rows} onOps={vi.fn()} />);
  // Disabled rather than wrapping: a control that silently sends a row to the far end is worse
  // than one that says it cannot move.
  expect(screen.getAllByTestId("objective-up")[0]).toBeDisabled();
  expect(screen.getAllByTestId("objective-down")[1]).toBeDisabled();
  expect(screen.getAllByTestId("objective-up")[1]).toBeEnabled();
});

test("read-only hides the reorder and rename controls too", () => {
  render(<MissionObjectives objectives={[obj()]} />);
  expect(screen.queryByTestId("objective-up")).toBeNull();
  expect(screen.queryByTestId("objective-rename")).toBeNull();
});
