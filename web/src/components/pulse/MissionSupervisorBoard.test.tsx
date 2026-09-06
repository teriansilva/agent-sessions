/** The supervisor's follow-through board (#885).
 *
 * Pinned, each mutation-tested against an implementation that would otherwise pass:
 *
 *  - an ABSENT assessment says so, and is NOT rendered as an empty/clean board — "we could not
 *    look" and "there is nothing to follow up" are different claims and the operator acts
 *    differently on each. This is the assertion that fails if the route's `contextlib.suppress`
 *    ever starts substituting an empty reading;
 *  - a mission with NO objectives is *unmeasured*, a third state distinct from both of the above;
 *  - the refusal SENTENCE comes from the server verbatim — the client never authors its own copy
 *    of that vocabulary, so a test that only asserted "some explanation is shown" would pass
 *    against a client-side re-derivation and is not what this asserts;
 *  - classification order: a stand-down outranks a spent budget (the operator's instruction, not
 *    a limit we hit), and MET outranks everything;
 *  - `likely_done` renders as a PROPOSAL — the words say nothing was closed.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { MissionSupervisor, SupervisorObjective } from "../../types/api";
import { MissionSupervisorBoard } from "./MissionSupervisorBoard";
import { boardFor } from "./supervisorBoard";

function obj(over: Partial<SupervisorObjective> = {}): SupervisorObjective {
  return {
    key: "k1",
    title: "Ship the thing",
    gate: false,
    state: "open",
    met: false,
    episode: 1,
    stood_down: false,
    awaiting_answer: false,
    spent: 0,
    remaining: 3,
    may_nudge: true,
    unreadable: false,
    indeterminate: false,
    live: 0,
    terminal: false,
    why_not: "",
    ...over,
  };
}

function sup(
  objectives: SupervisorObjective[],
  over: Partial<MissionSupervisor> = {},
): MissionSupervisor {
  return {
    objectives,
    likely_done: false,
    unmet_gates: 0,
    checked_at: 1,
    ...over,
  };
}

test("an absent assessment is reported, not rendered as an empty board", () => {
  render(<MissionSupervisorBoard supervisor={undefined} />);
  expect(screen.getByTestId("supervisor-unreadable")).toBeTruthy();
  // The distinguishing assertion: it must NOT claim there is nothing to follow up.
  expect(screen.queryByTestId("supervisor-board")).toBeNull();
  expect(screen.queryByTestId("supervisor-unmeasured")).toBeNull();
  expect(screen.getByTestId("supervisor-unreadable").textContent).toContain(
    "not a claim that there is nothing to follow up",
  );
});

test("no objectives is UNMEASURED — a third state, not the absent one and not done", () => {
  render(<MissionSupervisorBoard supervisor={sup([])} />);
  const el = screen.getByTestId("supervisor-unmeasured");
  expect(el.textContent).toContain("not the same as done");
  expect(screen.queryByTestId("supervisor-unreadable")).toBeNull();
});

test("the refusal sentence is the SERVER'S, rendered verbatim", () => {
  // A sentence no client-side re-derivation could produce: if the component ever authors its own
  // vocabulary this fails, where "some explanation is shown" would not.
  const sentence =
    "a previous nudge may or may not have been delivered; not sending another";
  render(
    <MissionSupervisorBoard
      supervisor={sup([
        obj({ may_nudge: false, remaining: 2, why_not: sentence }),
      ])}
    />,
  );
  expect(screen.getByText(sentence)).toBeTruthy();
});

test("a stand-down outranks a spent budget, and MET outranks everything", () => {
  // Both flags set at once: only the ordering decides, so a wrong order is caught rather than
  // being hidden by inputs that agree.
  expect(
    boardFor(obj({ stood_down: true, remaining: 0, may_nudge: false })),
  ).toBe("held");
  expect(
    boardFor(
      obj({ met: true, stood_down: true, remaining: 0, may_nudge: false }),
    ),
  ).toBe("met");
  expect(boardFor(obj({ remaining: 0, may_nudge: false }))).toBe("spent");
  expect(boardFor(obj({ remaining: 2, may_nudge: false }))).toBe("waiting");
  expect(boardFor(obj({ remaining: 2, may_nudge: true }))).toBe("ready");

  // AN OPEN QUESTION IS THE OPERATOR'S TURN, and the board said the opposite. The supervisor
  // stands an objective down while a question is open, so `may_nudge` is false and this fell
  // through to `waiting` — which on this board means the AGENT is being given room. On the one
  // page whose job is "what needs me right now", an objective waiting on the operator's own
  // answer read as one they had nothing to do about.
  expect(
    boardFor(obj({ awaiting_answer: true, may_nudge: false, remaining: 2 })),
  ).toBe("asked");
  // …and it outranks the ABSENCES, not the operator's own instruction. An unreadable ledger or a
  // spent budget does not change who owes the next move; a stand-down is the operator saying they
  // do not want to be asked, so a question inside one is not something to act on.
  expect(
    boardFor(
      obj({
        awaiting_answer: true,
        may_nudge: false,
        unreadable: true,
        remaining: 0,
      }),
    ),
  ).toBe("asked");
  expect(
    boardFor(
      obj({ awaiting_answer: true, stood_down: true, may_nudge: false }),
    ),
  ).toBe("held");
});

test("each board renders its own badge and marks the row", () => {
  render(
    <MissionSupervisorBoard
      supervisor={sup([
        obj({
          key: "a",
          title: "Held one",
          stood_down: true,
          may_nudge: false,
          why_not:
            "the operator asked not to be told about this objective again",
        }),
        obj({
          key: "b",
          title: "Spent one",
          remaining: 0,
          spent: 3,
          may_nudge: false,
          why_not: "the 3-nudge budget for this episode is spent",
        }),
        obj({ key: "c", title: "Ready one" }),
      ])}
    />,
  );
  const rows = screen.getAllByRole("listitem");
  expect(rows.map((r) => r.getAttribute("data-board"))).toEqual([
    "held",
    "spent",
    "ready",
  ]);
  // The spent row shows the budget it exhausted, not a bare flag.
  expect(within(rows[1]).getByText("3/3")).toBeTruthy();
  // A free objective carries no refusal text at all.
  expect(within(rows[2]).queryByText(/budget|operator asked/)).toBeNull();
});

test("likely_done is a proposal — it says nothing was closed", () => {
  render(
    <MissionSupervisorBoard
      supervisor={sup([obj({ met: true, gate: true })], { likely_done: true })}
    />,
  );
  const el = screen.getByTestId("supervisor-likely-done");
  expect(el.textContent).toContain("Nothing has been closed");
  expect(el.textContent).toContain("the call is yours");
});

test("unmet gates are counted, and singular/plural is not a lie", () => {
  const one = render(
    <MissionSupervisorBoard
      supervisor={sup([obj({ gate: true })], { unmet_gates: 1 })}
    />,
  );
  expect(screen.getByTestId("supervisor-unmet-gates").textContent).toBe(
    "1 unmet gate",
  );
  one.unmount();
  render(
    <MissionSupervisorBoard
      supervisor={sup([obj({ gate: true })], { unmet_gates: 2 })}
    />,
  );
  expect(screen.getByTestId("supervisor-unmet-gates").textContent).toBe(
    "2 unmet gates",
  );
});

test("an UNREADABLE ledger is UNKNOWN, never SPENT (#888 review, finding 11)", () => {
  // The server reports `remaining: 0` here because no budget can be justified from a file it could
  // not open — so a classifier reading the NUMBER calls it SPENT while the sentence beside it says
  // the budget is unknown. The discriminator is carried structurally for exactly this reason.
  const o = obj({
    may_nudge: false,
    remaining: 0,
    unreadable: true,
    why_not: "the action ledger could not be read, so the budget is unknown",
  });
  expect(boardFor(o)).toBe("unknown");

  render(<MissionSupervisorBoard supervisor={sup([o])} />);
  const row = screen.getAllByRole("listitem")[0];
  expect(row.getAttribute("data-board")).toBe("unknown");
  // The badge and the sentence must not contradict each other on screen.
  expect(within(row).queryByText("SPENT")).toBeNull();
  expect(within(row).getByText("UNKNOWN")).toBeTruthy();
});

test("a genuinely spent budget is still SPENT — the fix must not swallow the real case", () => {
  expect(
    boardFor(
      obj({ may_nudge: false, remaining: 0, spent: 3, unreadable: false }),
    ),
  ).toBe("spent");
});

// ---- the stand-down control (#889) ----------------------------------------------------------

test("STAND DOWN sends the episode the row was RENDERED at, never 'whatever is current'", async () => {
  const onStandDown = vi.fn();
  render(
    <MissionSupervisorBoard
      supervisor={{
        objectives: [
          obj({ key: "checks", episode: 4, stood_down: false, met: false }),
        ],
        likely_done: false,
        unmet_gates: 1,
        checked_at: 1,
      }}
      onStandDown={onStandDown}
    />,
  );
  const btn = screen.getByTestId("objective-stand-down");
  // Carried on the element too, so the browser gate can read it without reaching into React.
  expect(btn).toHaveAttribute("data-episode", "4");
  await userEvent.click(btn);
  // The board the operator tapped was rendered against episode 4. If the objective has moved
  // since, that tap is about a situation that no longer exists and the server answers 409 — a
  // client that sent the CURRENT episode would silence a report nobody has seen.
  expect(onStandDown).toHaveBeenCalledWith("checks", 4);
});

test("an objective already stood down offers no second stand-down", () => {
  render(
    <MissionSupervisorBoard
      supervisor={{
        objectives: [obj({ key: "checks", stood_down: true })],
        likely_done: false,
        unmet_gates: 1,
        checked_at: 1,
      }}
      onStandDown={vi.fn()}
    />,
  );
  // HELD is the state this control produces; offering it again suggests a second thing to do
  // that does not exist.
  expect(screen.queryByTestId("objective-stand-down")).toBeNull();
});

test("a met objective offers no stand-down — silencing it would have no effect", () => {
  render(
    <MissionSupervisorBoard
      supervisor={{
        objectives: [obj({ key: "checks", met: true, state: "met" })],
        likely_done: true,
        unmet_gates: 0,
        checked_at: 1,
      }}
      onStandDown={vi.fn()}
    />,
  );
  expect(screen.queryByTestId("objective-stand-down")).toBeNull();
});

test("without `onStandDown` the board is read-only", () => {
  render(
    <MissionSupervisorBoard
      supervisor={{
        objectives: [obj({ key: "checks" })],
        likely_done: false,
        unmet_gates: 1,
        checked_at: 1,
      }}
    />,
  );
  expect(screen.queryByTestId("objective-stand-down")).toBeNull();
  // …and the row still renders its board. Read-only is not "hidden".
  expect(screen.getByTestId("supervisor-board")).toBeInTheDocument();
});

test("a mission with NO SESSION says so instead of showing a board it cannot act on", () => {
  // Releasing the last session leaves a `running` mission the supervisor cannot act on: it
  // iterates held sessions and there are none. The server refuses every nudge for that reason,
  // so the rows are not READY — and the board names the cause once rather than leaving the
  // operator to infer it from five identical refusals (#896 review 7, finding 2).
  render(
    <MissionSupervisorBoard
      supervisor={sup(
        [
          obj({
            key: "checks",
            may_nudge: false,
            why_not:
              "this mission holds no session, so there is nothing to nudge",
          }),
        ],
        { unmet_gates: 1, held_sessions: 0, no_session: true },
      )}
    />,
  );
  const notice = screen.getByTestId("supervisor-no-session");
  expect(notice.textContent).toContain("holds no session");
  // …and no row claims the supervisor is about to act.
  expect(screen.queryByText("READY")).toBeNull();
  expect(screen.getByTestId("supervisor-board")).toBeInTheDocument();
});

test("a mission that HOLDS a session shows no such notice", () => {
  render(
    <MissionSupervisorBoard
      supervisor={sup([obj({ key: "checks", may_nudge: true })], {
        unmet_gates: 1,
        held_sessions: 1,
        no_session: false,
      })}
    />,
  );
  expect(screen.queryByTestId("supervisor-no-session")).toBeNull();
  expect(screen.getByText("READY")).toBeInTheDocument();
});

test("an UNREADABLE roster is not rendered as an empty one", () => {
  // Two different claims, and the operator acts differently on each: one is "adopt a session",
  // the other is "the store is unwell" (#896 review 8, finding 2).
  render(
    <MissionSupervisorBoard
      supervisor={sup(
        [
          obj({
            key: "checks",
            may_nudge: false,
            why_not:
              "the mission's sessions could not be read, so nothing may be sent",
          }),
        ],
        {
          unmet_gates: 1,
          held_sessions: null,
          no_session: false,
          sessions_unreadable: true,
        },
      )}
    />,
  );
  const notice = screen.getByTestId("supervisor-sessions-unreadable");
  expect(notice.textContent).toContain("not a claim");
  // …and it does NOT say the mission holds none.
  expect(screen.queryByTestId("supervisor-no-session")).toBeNull();
  expect(screen.queryByText("READY")).toBeNull();
});
