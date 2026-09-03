/** The durable turn's client half (#890).
 *
 * The properties worth pinning are the ones the route's whole shape exists for, and none of them
 * are visible in the rendered output alone — they are on the WIRE and in the ordering:
 *
 *  - the `turn_id` is stable across a retry, or the idempotency is decorative;
 *  - `indeterminate` is never retried on its own;
 *  - a completion for a mission the operator has left is discarded, not filed.
 */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import type { Mission, MissionOpenTurn } from "../../types/api";

import { MissionComposer } from "./MissionComposer";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { missionMessage: vi.fn(), ackMissionTurn: vi.fn() },
  };
});

function panel(over: Partial<Parameters<typeof MissionComposer>[0]> = {}) {
  const props = {
    missionId: "msn_a",
    configured: true,
    isCurrent: () => true,
    onSettled: () => {},
    detail: null,
    ...over,
  };
  return render(<MissionComposer {...props} />);
}

async function send(text: string) {
  await userEvent.type(screen.getByTestId("composer-input"), text);
  await userEvent.click(screen.getByTestId("composer-send"));
}

beforeEach(() => {
  vi.mocked(api.missionMessage).mockReset();
  vi.mocked(api.ackMissionTurn).mockReset().mockResolvedValue({
    turn_id: "x",
    acked: true,
  });
});

test("a RETRY reuses the same turn_id", async () => {
  // THE WHOLE POINT of the route's shape. A fresh id per attempt makes every retry a new model
  // execution — the double-instruct #871 was built to prevent — so the id has to be minted once
  // per send and kept with the pending turn. Asserted on the WIRE, because the rendered output
  // looks identical either way.
  vi.mocked(api.missionMessage)
    .mockRejectedValueOnce(new ApiError(502, "the chat backend failed"))
    .mockResolvedValueOnce({ turn_id: "x", state: "done", answer: "ok" });

  panel();
  await send("what happened here?");
  await waitFor(() =>
    expect(screen.getByTestId("turn-error")).toBeInTheDocument(),
  );

  await userEvent.click(screen.getByTestId("turn-retry"));
  await waitFor(() => expect(api.missionMessage).toHaveBeenCalledTimes(2));

  const first = vi.mocked(api.missionMessage).mock.calls[0][1];
  const second = vi.mocked(api.missionMessage).mock.calls[1][1];
  expect(second.turnId).toBe(first.turnId);
  expect(second.message).toBe(first.message);
});

test("a NEW message gets a new turn_id", async () => {
  // The mirror: stability across a retry must not become one id for the mission's whole life,
  // which would make the second question replay the first one's stored answer.
  vi.mocked(api.missionMessage).mockResolvedValue({
    turn_id: "x",
    state: "done",
    answer: "ok",
  });
  panel();
  await send("first");
  await waitFor(() => expect(api.missionMessage).toHaveBeenCalledTimes(1));
  await send("second");
  await waitFor(() => expect(api.missionMessage).toHaveBeenCalledTimes(2));
  const a = vi.mocked(api.missionMessage).mock.calls[0][1];
  const b = vi.mocked(api.missionMessage).mock.calls[1][1];
  expect(b.turnId).not.toBe(a.turnId);
});

test("INDETERMINATE is never retried on its own", async () => {
  // The route answers it precisely when nobody can say whether the instruction went out. An
  // automatic retry could be a second copy of an instruction the agent already has, so the
  // operator is told that and decides.
  vi.mocked(api.missionMessage).mockResolvedValue({
    turn_id: "x",
    state: "indeterminate",
    delivery_error: "TimeoutError",
  });
  panel();
  await send("tell it to run the tests");
  await waitFor(() =>
    expect(screen.getByTestId("turn-indeterminate")).toBeInTheDocument(),
  );
  // …and nothing happened by itself.
  await new Promise((r) => setTimeout(r, 60));
  expect(api.missionMessage).toHaveBeenCalledTimes(1);
  // The operator's choice is offered, and it is explicit about what it does.
  expect(screen.getByTestId("turn-indeterminate")).toHaveTextContent(
    /will not run\s+a second time/,
  );
});

test("IN PROGRESS is shown as a server fact, not as a spinner that ended", async () => {
  vi.mocked(api.missionMessage).mockResolvedValue({
    turn_id: "x",
    state: "in_progress",
  });
  panel();
  await send("what is it doing?");
  await waitFor(() =>
    expect(screen.getByTestId("turn-in-progress")).toBeInTheDocument(),
  );
  expect(screen.queryByTestId("turn-error")).toBeNull();
});

test("a completion for a mission the operator LEFT is discarded", async () => {
  // #878's rule, inherited verbatim: the request is not abortable, so keying the subtree on the
  // mission is not enough on its own. Liveness is read at RESOLUTION time — a boolean captured
  // when the request started answers the question as it was at the moment that does not matter.
  let release: ((v: unknown) => void) | null = null;
  vi.mocked(api.missionMessage).mockImplementation(
    () => new Promise((r) => (release = r)) as never,
  );
  let current = true;
  const settled: number[] = [];
  panel({ isCurrent: () => current, onSettled: () => settled.push(1) });

  await send("ask something");
  await waitFor(() => expect(release).not.toBeNull());

  current = false; // the operator selects a different mission
  await act(async () => {
    release?.({ turn_id: "x", state: "done", answer: "an answer for A" });
    await Promise.resolve();
  });

  expect(screen.queryByText("an answer for A")).toBeNull();
  expect(settled).toEqual([]);
});

test("the server's own refusal is shown as written", async () => {
  // "turn_id was already used for a different message" and "a question is already running" are
  // different problems with different fixes. They are authored, short and carry no mission
  // content — which is why they are shown rather than flattened (#834, #871).
  vi.mocked(api.missionMessage).mockRejectedValue(
    new ApiError(409, "a question is already running"),
  );
  panel();
  await send("hello");
  await waitFor(() =>
    expect(screen.getByTestId("turn-error")).toHaveTextContent(
      "a question is already running",
    ),
  );
});

test("no AI endpoint disables the control and says why", async () => {
  panel({ configured: false });
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  expect(screen.getByTestId("composer-input")).toHaveAttribute(
    "placeholder",
    "Needs an AI endpoint",
  );
});

// ==============================================================================================
// The DURABLE half: what a reload finds, and what happens when the store and this component
// disagree (#902 review, findings 1 and 2).
// ==============================================================================================

/** A mission row as the detail read returns it — a NEW object each time, which is what tells the
 *  composer the server has spoken even when the answer is "no open turn". */
function withTurn(turn: MissionOpenTurn | null) {
  return { id: "msn_a", turn } as unknown as Mission;
}

const OPEN = {
  turn_id: "t-1",
  state: "in_progress" as const,
  text: "what is it doing?",
  delivery_error: "",
  created_at: 1,
};

test("a RELOAD finds the running turn, with nothing sent from this component", () => {
  // The state that used to live in a React ref and die with the tab. Nothing is posted here —
  // this is a fresh mount reading the mission detail, which is exactly what a reload is.
  panel({ detail: withTurn(OPEN) });
  expect(screen.getByTestId("turn-in-progress")).toBeInTheDocument();
  expect(api.missionMessage).not.toHaveBeenCalled();
});

test("a RELOAD finds an AMBIGUOUS turn, with its stable id and CHECK AGAIN", async () => {
  // The one that matters most: `indeterminate` exists precisely because nobody can say whether
  // the instruction went out, and the decision is the operator's. Reloading used to turn it into
  // an ordinary settled Answer with no way to ask again — the ambiguity silently resolved itself
  // in the operator's favour, which is the opposite of what the state means.
  vi.mocked(api.missionMessage).mockResolvedValue({
    turn_id: "t-1",
    state: "indeterminate",
  });
  panel({ detail: withTurn({ ...OPEN, state: "indeterminate" }) });
  expect(screen.getByTestId("turn-indeterminate")).toBeInTheDocument();

  await userEvent.click(screen.getByTestId("turn-recheck"));
  await waitFor(() => expect(api.missionMessage).toHaveBeenCalledTimes(1));
  // The SERVER's turn id, not a fresh one — a new id would be a second execution of an
  // instruction the agent may already have.
  expect(vi.mocked(api.missionMessage).mock.calls[0][1]).toEqual({
    message: "what is it doing?",
    turnId: "t-1",
  });
});

test("the operator's message is NOT echoed once the store knows the turn", () => {
  // The claim writes an `operator_msg` event, the thread renders it, and a local copy beside it
  // is one message on screen twice. The review reproduced three matching rows.
  panel({ detail: withTurn(OPEN) });
  expect(screen.queryByText("what is it doing?")).toBeNull();
});

test("a SETTLED turn clears the local copy instead of talking over the answer", async () => {
  // The other direction: the poll sees the settlement, the timeline prints the answer, and an
  // unreconciled local `pending` goes on saying the same turn is still running.
  vi.mocked(api.missionMessage).mockResolvedValue({
    turn_id: "t-1",
    state: "in_progress",
  });
  const { rerender } = panel();
  await send("what is it doing?");
  await waitFor(() =>
    expect(screen.getByTestId("turn-in-progress")).toBeInTheDocument(),
  );

  // The detail read comes back with no open turn: the server settled it.
  rerender(
    <MissionComposer
      missionId="msn_a"
      configured
      isCurrent={() => true}
      onSettled={() => {}}
      detail={withTurn(null)}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByTestId("turn-in-progress")).toBeNull(),
  );
  expect(screen.queryByTestId("turn-pending")).toBeNull();
});

test("a RUNNING turn blocks the next send; an AMBIGUOUS one does not", async () => {
  const { rerender } = panel({ detail: withTurn(OPEN) });
  await userEvent.type(screen.getByTestId("composer-input"), "and another");
  expect(screen.getByTestId("composer-send")).toBeDisabled();

  rerender(
    <MissionComposer
      missionId="msn_a"
      configured
      isCurrent={() => true}
      onSettled={() => {}}
      detail={withTurn({ ...OPEN, state: "indeterminate" })}
    />,
  );
  // Terminal, and the copy tells the operator to write a new message — so it must be possible.
  expect(screen.getByTestId("composer-send")).not.toBeDisabled();
});

test("DISMISS is a DURABLE acknowledgement, not a local hide", async () => {
  // Clearing only the local copy puts the banner back on the next reload: the same bug one level
  // down, a decision kept somewhere that does not survive the tab closing.
  const settled = vi.fn();
  panel({
    detail: withTurn({ ...OPEN, state: "indeterminate" }),
    onSettled: settled,
  });
  await userEvent.click(screen.getByTestId("turn-dismiss"));
  await waitFor(() => expect(api.ackMissionTurn).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.ackMissionTurn).mock.calls[0]).toEqual(["msn_a", "t-1"]);
  expect(settled).toHaveBeenCalled();
});

test("a DISMISS that did not land does not look like one that did", async () => {
  vi.mocked(api.ackMissionTurn).mockRejectedValue(new ApiError(500, "nope"));
  const settled = vi.fn();
  panel({
    detail: withTurn({ ...OPEN, state: "indeterminate" }),
    onSettled: settled,
  });
  await userEvent.click(screen.getByTestId("turn-dismiss"));
  await waitFor(() => expect(api.ackMissionTurn).toHaveBeenCalledTimes(1));
  expect(settled).not.toHaveBeenCalled();
  expect(screen.getByTestId("turn-indeterminate")).toBeInTheDocument();
});

test("a LOST RESPONSE over a committed turn is reconciled, not left as TRY AGAIN", async () => {
  // "My request failed" and "the turn never happened" are different claims, and the client can
  // only tell them apart from the durable record: the server can commit and settle a turn and
  // the response can still be lost. Without the discriminator the composer showed TRY AGAIN
  // beside an answer the server had already stored, for ever (#902 review 2, finding 1).
  vi.mocked(api.missionMessage).mockRejectedValue(
    new ApiError(502, "gateway went away"),
  );
  const { rerender } = panel();
  await send("run the tests");
  await waitFor(() =>
    expect(screen.getByTestId("turn-error")).toBeInTheDocument(),
  );
  const turnId = vi.mocked(api.missionMessage).mock.calls[0][1].turnId;

  // The next detail read: no open turn, and the ANSWER carries the turn's own id.
  rerender(
    <MissionComposer
      missionId="msn_a"
      configured
      isCurrent={() => true}
      onSettled={() => {}}
      detail={
        {
          id: "msn_a",
          turn: null,
          events: [
            {
              seq: 2,
              kind: "assistant_msg",
              text: "all green",
              meta: { turn_id: turnId },
            },
          ],
        } as unknown as Mission
      }
    />,
  );
  await waitFor(() => expect(screen.queryByTestId("turn-error")).toBeNull());
  expect(screen.queryByTestId("turn-pending")).toBeNull();
});

test("a failure with NO durable answer keeps TRY AGAIN", async () => {
  // The mirror, and the reason this is a discriminator rather than "clear on any reload": a
  // claim that never landed leaves nothing on the timeline, and TRY AGAIN is then exactly the
  // control the operator needs.
  vi.mocked(api.missionMessage).mockRejectedValue(
    new ApiError(502, "gateway went away"),
  );
  const { rerender } = panel();
  await send("run the tests");
  await waitFor(() =>
    expect(screen.getByTestId("turn-error")).toBeInTheDocument(),
  );

  rerender(
    <MissionComposer
      missionId="msn_a"
      configured
      isCurrent={() => true}
      onSettled={() => {}}
      detail={{ id: "msn_a", turn: null, events: [] } as unknown as Mission}
    />,
  );
  expect(screen.getByTestId("turn-error")).toBeInTheDocument();
});

test("a failure the SERVER still has as an open turn shows the server's state", async () => {
  // The response was lost while the turn is genuinely still running. "Still working" is the
  // truth; TRY AGAIN would offer to run it a second time.
  vi.mocked(api.missionMessage).mockRejectedValue(
    new ApiError(502, "gateway went away"),
  );
  const { rerender } = panel();
  await send("run the tests");
  await waitFor(() =>
    expect(screen.getByTestId("turn-error")).toBeInTheDocument(),
  );
  const turnId = vi.mocked(api.missionMessage).mock.calls[0][1].turnId;

  rerender(
    <MissionComposer
      missionId="msn_a"
      configured
      isCurrent={() => true}
      onSettled={() => {}}
      detail={withTurn({ ...OPEN, turn_id: turnId })}
    />,
  );
  expect(screen.getByTestId("turn-in-progress")).toBeInTheDocument();
  expect(screen.queryByTestId("turn-error")).toBeNull();
});
