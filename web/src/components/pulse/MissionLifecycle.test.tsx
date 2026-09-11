/** The mission's lifecycle controls (#889).
 *
 *  The case this file exists for is the FIRST one: a transition whose compare-and-set lost must
 *  render what the mission actually is, never what the operator asked for. That is the console's
 *  half of the store's `from`-comparand contract, and it is the difference between a console that
 *  reports facts and one that reports intentions.
 *
 *  Each test is mutation-checked in the PR: the 409 case fails against an implementation that
 *  applies the requested state optimistically, and the archive cases fail against one that sends
 *  a constant `abandon`.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import type { Mission } from "../../types/api";

import { MissionLifecycle } from "./MissionLifecycle";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      setMissionState: vi.fn(),
      archiveMission: vi.fn(),
      unarchiveMission: vi.fn(),
    },
  };
});

function mission(over: Partial<Mission> = {}): Mission {
  return {
    id: "msn_1",
    title: "t",
    instruction: null,
    brief: null,
    project_id: "p1",
    cwd: "/repo",
    engine: null,
    engine_source: null,
    state: "running",
    playbook_id: null,
    created_at: 1,
    updated_at: 1,
    closed_at: null,
    archived_at: null,
    archiving_at: null,
    unarchiving_at: null,
    outcome: null,
    sessions: [{ session_key: "claude:a", removed_at: null }],
    ...over,
  } as Mission;
}

beforeEach(() => {
  vi.mocked(api.setMissionState).mockReset();
  vi.mocked(api.archiveMission).mockReset();
  vi.mocked(api.unarchiveMission).mockReset();
});

/** Open the secondary-lifecycle menu (#942).
 *
 *  ARCHIVE, ABANDON, MARK FAILED and NOT YET moved behind `⋯` — four controls at near-equal
 *  weight, two destructive, left the operator no primary to aim at. They kept their testids and
 *  their confirmations; the only change a test needs is to open the menu, which is what an
 *  operator now does too. */
async function openOverflow() {
  const t = screen.queryByTestId("mission-overflow");
  if (!t) return;
  // IDEMPOTENT, and it has to be (#942 review 3). Several tests call this twice — once to open
  // the menu and once more before the CONFIRM tap on the same item, which is already inside it.
  // While the trigger could not close what it opened, the second call looked like a no-op and
  // these tests passed on that bug; with the toggle working it correctly shuts the menu and the
  // item vanishes. So ask before pressing, the way an operator looking at the screen would.
  if (t.getAttribute("aria-expanded") === "true") return;
  await userEvent.click(t);
}

test("a transition sends the state the client BELIEVES, as the comparand", async () => {
  vi.mocked(api.setMissionState).mockResolvedValue(mission({ state: "done" }));
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  // TWO TAPS. Closing releases every session the mission holds, so #889 asks for a confirm and
  // the first click only asks (#896 review 6, finding 2).
  await userEvent.click(screen.getByTestId("mission-done"));
  expect(api.setMissionState).not.toHaveBeenCalled();
  await userEvent.click(screen.getByTestId("mission-done"));
  await waitFor(() => expect(api.setMissionState).toHaveBeenCalled());
  // `from` is the CURRENT state, not the target — the store compares it and a mismatch is the
  // lost race. Sending the target would make every CAS trivially succeed.
  expect(vi.mocked(api.setMissionState).mock.calls[0][1]).toEqual({
    from: "running",
    to: "done",
    outcome: "done",
  });
});

test("a LOST RACE re-reads instead of showing the state that was asked for", async () => {
  // The server's answer: this mission is not `running` any more, so the transition never
  // happened. A console that applied the request optimistically would now be showing `done` for
  // a mission somebody else closed as `failed`.
  vi.mocked(api.setMissionState).mockRejectedValue(
    new ApiError(409, "mission msn_1 is no longer running"),
  );
  const onChanged = vi.fn();
  const onNote = vi.fn();
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={onChanged}
      onNote={onNote}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-done"));
  await userEvent.click(screen.getByTestId("mission-done"));

  // 1. the server's own sentence reaches the operator …
  await waitFor(() =>
    expect(onNote).toHaveBeenCalledWith("mission msn_1 is no longer running"),
  );
  // 2. … and the console re-reads. THIS is the assertion that fails against an optimistic
  //    implementation: the component holds no state of its own to show, so the only way the
  //    next render can be right is for the parent to fetch it.
  expect(onChanged).toHaveBeenCalled();
  // 3. nothing on screen claims the transition landed.
  expect(screen.getByTestId("mission-state")).toHaveTextContent("running");
});

test("the re-read happens on SUCCESS too, not only on a refusal", async () => {
  vi.mocked(api.setMissionState).mockResolvedValue(mission({ state: "done" }));
  const onChanged = vi.fn();
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={onChanged}
      onNote={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-done"));
  await userEvent.click(screen.getByTestId("mission-done"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("planning states leave Begin and Re-plan to the shared plan owner", () => {
  render(
    <MissionLifecycle
      mission={mission({ state: "planned" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  expect(screen.queryByTestId("mission-begin")).toBeNull();
  expect(screen.queryByTestId("mission-plan")).toBeNull();
});

test("archiving a LIVE mission confirms first, and says it will stop the agents", async () => {
  vi.mocked(api.archiveMission).mockResolvedValue({ mission: mission() });
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  // No call yet — the first tap only asks.
  expect(api.archiveMission).not.toHaveBeenCalled();
  expect(screen.getByTestId("mission-confirm-archive")).toHaveTextContent(
    /abandon it first/i,
  );
  expect(screen.getByTestId("mission-confirm-archive")).toHaveTextContent(
    /transcript is kept/i,
  );

  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  await waitFor(() => expect(api.archiveMission).toHaveBeenCalled());
  // `abandon` is a REAL boolean and it is true only because the mission is live. The route
  // type-checks it, and this is the flag that authorises terminating agents.
  expect(vi.mocked(api.archiveMission).mock.calls[0][1]).toEqual({
    abandon: true,
  });
});

test("archiving a CLOSED mission does not ask to abandon it", async () => {
  vi.mocked(api.archiveMission).mockResolvedValue({ mission: mission() });
  render(
    <MissionLifecycle
      mission={mission({ state: "done", outcome: "done" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  expect(screen.getByTestId("mission-confirm-archive")).not.toHaveTextContent(
    /abandon/i,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  await waitFor(() => expect(api.archiveMission).toHaveBeenCalled());
  expect(vi.mocked(api.archiveMission).mock.calls[0][1]).toEqual({
    abandon: false,
  });
});

test("an archived mission offers UNARCHIVE and nothing else", () => {
  render(
    <MissionLifecycle
      mission={mission({ state: "done", archived_at: 99 })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  expect(screen.getByTestId("mission-unarchive")).toBeInTheDocument();
  // Offering ABANDON or MARK DONE on an archived mission invites a write whose only effect is to
  // confuse the record of finished work.
  expect(screen.queryByTestId("mission-abandon")).toBeNull();
  expect(screen.queryByTestId("mission-done")).toBeNull();
  expect(screen.queryByTestId("mission-archive")).toBeNull();
});

test("abandon is confirmed, and says it cannot be undone", async () => {
  vi.mocked(api.setMissionState).mockResolvedValue(
    mission({ state: "abandoned" }),
  );
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-abandon"));
  expect(api.setMissionState).not.toHaveBeenCalled();
  expect(screen.getByTestId("mission-confirm-abandon")).toHaveTextContent(
    /cannot be reopened/i,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-abandon"));
  await waitFor(() => expect(api.setMissionState).toHaveBeenCalled());
  expect(vi.mocked(api.setMissionState).mock.calls[0][1]).toEqual({
    from: "running",
    to: "abandoned",
    outcome: "abandoned",
  });
});

test("only the transitions the server's graph allows are offered", () => {
  // `done` reopens to `running` and can be archived; it cannot be abandoned, and `_ALLOWED`
  // agrees ({"done": {"running"}}). An offered control the server refuses on the GRAPH (as
  // opposed to on a race) is a bug in this mirror, not a race.
  render(
    <MissionLifecycle
      mission={mission({ state: "done", outcome: "done" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  expect(screen.getByTestId("mission-reopen")).toBeInTheDocument();
  expect(screen.queryByTestId("mission-abandon")).toBeNull();
  expect(screen.queryByTestId("mission-done")).toBeNull();
  expect(screen.queryByTestId("mission-begin")).toBeNull();
});

test("MARK DONE confirms before it closes the mission", async () => {
  // #896 review 6, finding 2. #889 asks for "confirm done" in those words, and closing releases
  // every session the mission holds — an accidental tap changed session ownership immediately.
  vi.mocked(api.setMissionState).mockResolvedValue(mission({ state: "done" }));
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  const done = screen.getByTestId("mission-done");
  expect(done).toHaveTextContent(/^MARK DONE$/i);
  await userEvent.click(done);
  expect(api.setMissionState).not.toHaveBeenCalled();
  // …and the button SAYS the next tap is the one that does it.
  expect(screen.getByTestId("mission-done")).toHaveTextContent(/confirm/i);
  await userEvent.click(screen.getByTestId("mission-done"));
  await waitFor(() => expect(api.setMissionState).toHaveBeenCalled());
});

test("UNARCHIVE offers RECORD ONLY beside restarting the agents", async () => {
  // #896 review 6, finding 4. Unarchive restores sessions by RELAUNCHING them from their
  // transcripts, and one click sent `sessions: true` with nothing on screen saying so — an
  // operator who read UNARCHIVE as "put the record back" could start several agents.
  vi.mocked(api.unarchiveMission).mockResolvedValue({ mission: mission() });
  render(
    <MissionLifecycle
      mission={mission({ state: "done", archived_at: 99 })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-unarchive"));
  expect(api.unarchiveMission).not.toHaveBeenCalled();
  expect(screen.getByTestId("mission-confirm-unarchive")).toHaveTextContent(
    /restart its agents/i,
  );

  // The SAFE one is a real option, not a footnote.
  await userEvent.click(screen.getByTestId("mission-unarchive-record"));
  await waitFor(() => expect(api.unarchiveMission).toHaveBeenCalled());
  expect(vi.mocked(api.unarchiveMission).mock.calls[0][1]).toEqual({
    sessions: false,
  });
});

test("UNARCHIVE can still restart the agents, explicitly", async () => {
  vi.mocked(api.unarchiveMission).mockResolvedValue({ mission: mission() });
  render(
    <MissionLifecycle
      mission={mission({ state: "done", archived_at: 99 })}
      onChanged={() => {}}
      onNote={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-unarchive"));
  await userEvent.click(screen.getByTestId("mission-unarchive-sessions"));
  await waitFor(() => expect(api.unarchiveMission).toHaveBeenCalled());
  expect(vi.mocked(api.unarchiveMission).mock.calls[0][1]).toEqual({
    sessions: true,
  });
});

test("a LOST terminal CAS still reconciles the OVERVIEW, and still does not move the operator", async () => {
  // #896 review 10, finding 5. The effects were success-only as a whole, which is right for one
  // of them and wrong for the other.
  //
  // A 409 on `running -> done` says the mission is ALREADY in a state this client did not put it
  // in — and for a terminal state, the server released every session it held when that happened.
  // The overview's cards still carry the old `mission_id`, so suppressing the membership refresh
  // leaves those sessions in neither the roster nor UNTRACKED until the outer poll: an ownership
  // picture the server has just disproved, held on screen because OUR transition lost.
  vi.mocked(api.setMissionState).mockRejectedValue(
    new ApiError(409, "mission msn_1 is no longer running"),
  );
  const onChanged = vi.fn();
  render(
    <MissionLifecycle
      mission={mission({ state: "running" })}
      onChanged={onChanged}
      onNote={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-done"));
  await userEvent.click(screen.getByTestId("mission-done"));

  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const opts = onChanged.mock.calls.at(-1)?.[0] as
    | Record<string, unknown>
    | undefined;
  // THE GLOBAL half rides on the failure …
  expect(opts?.membershipChanged).toBe(true);
  // … and the VIEW-LOCAL half does not: a scope move clears the selection, so forwarding it
  // from a refusal would move the operator off the very mission whose error they were handed.
  expect(opts?.movedTo).toBeUndefined();
});

test("a FAILED archive reconciles membership too, but leaves the scope alone", async () => {
  // The same rule on the other control: archive releases sessions as well, and a refused archive
  // can only be refused because the mission is not what this client believed.
  vi.mocked(api.archiveMission).mockRejectedValue(
    new ApiError(409, "mission msn_1 is already archived"),
  );
  const onChanged = vi.fn();
  render(
    <MissionLifecycle
      mission={mission({ state: "done", closed_at: 2 })}
      onChanged={onChanged}
      onNote={() => {}}
    />,
  );
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  await openOverflow();
  await userEvent.click(screen.getByTestId("mission-archive"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const opts = onChanged.mock.calls.at(-1)?.[0] as
    | Record<string, unknown>
    | undefined;
  expect(opts?.membershipChanged).toBe(true);
  expect(opts?.movedTo).toBeUndefined();
});

test("UNARCHIVE names the scope the mission moved INTO", async () => {
  // #896 review 10, finding 6. "It left this rail" was enough while archive-from-active was the
  // only mover; unarchive is the same event in the other direction, and a console that answers
  // it by re-reading the rail on screen re-reads the one scope the mission has just left. The
  // destination is known HERE, by which button was pressed.
  vi.mocked(api.unarchiveMission).mockResolvedValue(
    mission({ archived_at: null }) as never,
  );
  const onChanged = vi.fn();
  render(
    <MissionLifecycle
      mission={mission({ state: "done", archived_at: 5 })}
      onChanged={onChanged}
      onNote={() => {}}
    />,
  );
  await userEvent.click(screen.getByTestId("mission-unarchive"));
  await userEvent.click(screen.getByTestId("mission-unarchive-record"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  // ONE FIELD (#896 review 14): "it moved" and "where it went" cannot be forwarded apart,
  // because there is nothing to forward apart.
  expect(onChanged.mock.calls.at(-1)?.[0]).toMatchObject({ movedTo: "active" });
});
