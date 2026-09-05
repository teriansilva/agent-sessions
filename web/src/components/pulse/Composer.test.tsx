/** The composer (#878) — where the Ask box's behaviour went, plus the lifecycle rule that only
 *  exists because the answers are transient.
 *
 *  Four of these descend directly from `Pulse.test.tsx`'s Ask cases (#522): the answer renders,
 *  a 409 surfaces the server's own detail rather than a generic error, a follow-up replays the
 *  prior turns as history, and an unconfigured endpoint disables the control and makes no call.
 *  The fifth is new and is the one this component exists to get right.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCallback, useEffect, useRef, useState } from "react";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../lib/api";

import { Composer, type AskTurn } from "./Composer";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      pulseAsk: vi.fn(),
      // NEW MISSION's two calls (#889). Present on the mock so a test that opens the mode does
      // not fall over on an undefined member — and so a component that calls the WRONG one
      // (`folders`, the picker these are not interchangeable with) fails loudly here.
      projectEntities: vi.fn(),
      createMission: vi.fn(),
    },
  };
});

/** A host that owns per-mission turns exactly as the console does, and can switch missions.
 *
 *  **What this harness can and cannot prove.** It supplies `key={missionId}` itself, mirroring
 *  the console — so these tests establish that GIVEN the keying, an answer files under the
 *  mission that asked. They cannot establish that the console actually keys anything: mutating
 *  the console's `key` away leaves every test here green, because this file never renders the
 *  console. That half is asserted where it lives, in `routes/Pulse.test.tsx`
 *  ("switching missions starts a clean composer"), and the two together are the guarantee.
 *
 *  Saying so explicitly because a harness that bakes in the mechanism under test is the classic
 *  way a suite stays green against unfixed code. */
function Host({ initial = "m1" }: { initial?: string }) {
  const [missionId, setMissionId] = useState(initial);
  const [turns, setTurns] = useState<Record<string, AskTurn[]>>({});
  // THE CONSOLE'S VISIT COUNTER, in a REF exactly as the console keeps it. A closure over the
  // current visit would be captured by the composer's last render and answer "yes, still
  // current" for ever — which is the question this file exists to ask, so getting it wrong here
  // would make every test pass against a component that discards nothing.
  const visitRef = useRef(0);
  useEffect(() => {
    visitRef.current += 1;
  }, [missionId]);
  const visit = useCallback(() => visitRef.current, []);
  const isVisitCurrent = useCallback(
    (at: number) => at === visitRef.current,
    [],
  );
  return (
    <>
      <button type="button" onClick={() => setMissionId("m2")}>
        switch
      </button>
      <button type="button" onClick={() => setMissionId("m1")}>
        back
      </button>
      <div data-testid="which">{missionId}</div>
      <Composer
        key={missionId}
        missionId={missionId}
        configured
        turns={turns[missionId] ?? []}
        onTurns={(id, fn) =>
          setTurns((prev) => ({ ...prev, [id]: fn(prev[id] ?? []) }))
        }
        visit={visit}
        isVisitCurrent={isVisitCurrent}
        onCreated={() => {}}
      />
      {/* Renders BOTH missions' turn counts, so a mis-filed answer is visible rather than
          merely absent from the mission on screen. */}
      <div data-testid="count-m1">{(turns.m1 ?? []).length}</div>
      <div data-testid="count-m2">{(turns.m2 ?? []).length}</div>
      <div data-testid="answers-m1">
        {(turns.m1 ?? []).map((t) => t.answer ?? "").join("|")}
      </div>
      <div data-testid="answers-m2">
        {(turns.m2 ?? []).map((t) => t.answer ?? "").join("|")}
      </div>
    </>
  );
}

beforeEach(() => {
  vi.mocked(api.pulseAsk).mockReset();
});

test("a question renders its answer in the thread (#522)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "You merged it on Tuesday.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  render(<Host />);
  await userEvent.type(
    screen.getByTestId("composer-input"),
    "when did I merge it",
  );
  await userEvent.click(screen.getByTestId("composer-send"));
  // Scoped to the thread: the harness also mirrors both missions' answers for the switch test,
  // so an unscoped query matches the mirror as well as the real rendering.
  const turns = await screen.findByTestId("ask-turns");
  expect(
    within(turns).getByText("You merged it on Tuesday."),
  ).toBeInTheDocument();
});

test("a busy 409 surfaces the server's detail, not a generic error (#522)", async () => {
  // The detail IS the answer here — "a question is already running" tells the operator to wait,
  // where "that didn't work" tells them to retry, which is the wrong move.
  vi.mocked(api.pulseAsk).mockRejectedValue(
    new ApiError(409, "a question is already running"),
  );
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "hello");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(await screen.findByTestId("ask-error")).toHaveTextContent(
    /a question is already running/i,
  );
});

test("a follow-up replays the prior turns as history (#522)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "First answer.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "one");
  await userEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() =>
    expect(
      within(screen.getByTestId("ask-turns")).getByText("First answer."),
    ).toBeInTheDocument(),
  );

  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "Second answer.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  await userEvent.type(screen.getByTestId("composer-input"), "two");
  await userEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() =>
    expect(
      within(screen.getByTestId("ask-turns")).getByText("Second answer."),
    ).toBeInTheDocument(),
  );

  expect(api.pulseAsk).toHaveBeenLastCalledWith("two", [
    { role: "user", content: "one" },
    { role: "assistant", content: "First answer." },
  ]);
});

test("an unconfigured endpoint disables the control and makes no call (#522)", async () => {
  render(
    <Composer
      missionId="m1"
      configured={false}
      turns={[]}
      onTurns={() => {}}
      visit={() => 0}
      isVisitCurrent={() => true}
      onCreated={() => {}}
    />,
  );
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(api.pulseAsk).not.toHaveBeenCalled();
});

test("the thread says these answers are not kept (#878, until #871)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "ok",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "x");
  await userEvent.click(screen.getByTestId("composer-send"));
  // The operator is told, rather than discovering it by reloading and finding nothing.
  expect(await screen.findByTestId("ask-transient")).toHaveTextContent(
    /not kept|disappear/i,
  );
});

test("a reply that lands AFTER a mission switch is DISCARDED — by both missions", async () => {
  // #878's contract, and it is stronger than "does not land in B". A completion whose mission is
  // no longer selected is state the operator never asked to keep: filing it under A means they
  // meet it later with no context for it. So neither mission receives it, and the pending turn
  // goes too rather than being left half-finished.
  //
  // Driven through the real component with a genuinely deferred promise, not by unit testing a
  // helper — the bug lives entirely in WHICH component state a late resolution writes to.
  let resolveIt: ((v: unknown) => void) | undefined;
  vi.mocked(api.pulseAsk).mockReturnValue(
    new Promise((res) => {
      resolveIt = res;
    }) as ReturnType<typeof api.pulseAsk>,
  );

  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "for mission one");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(screen.getByTestId("count-m1")).toHaveTextContent("1");

  // …the operator moves on before the answer comes back.
  await userEvent.click(screen.getByRole("button", { name: "switch" }));
  expect(screen.getByTestId("which")).toHaveTextContent("m2");
  expect(screen.getByTestId("count-m2")).toHaveTextContent("0");

  resolveIt?.({
    answer: "Mission one's answer.",
    matches: [],
    stage: "catalog",
    configured: true,
  });

  // A's pending turn is withdrawn rather than completed…
  await waitFor(() =>
    expect(screen.getByTestId("count-m1")).toHaveTextContent("0"),
  );
  expect(screen.getByTestId("answers-m1")).toHaveTextContent("");
  // …and B never sees it either — not the answer, and not a turn.
  expect(screen.getByTestId("count-m2")).toHaveTextContent("0");
  expect(screen.getByTestId("answers-m2")).toHaveTextContent("");
});

test("mission two's composer does not inherit mission one's in-flight busy state", async () => {
  // The quieter half of the same lie: a spinner belongs to the mission that asked. If `busy`
  // survived the switch, B's SEND would be disabled for a request B never made.
  vi.mocked(api.pulseAsk).mockReturnValue(
    new Promise(() => {}) as ReturnType<typeof api.pulseAsk>,
  );
  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "hanging");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(screen.getByTestId("composer-send")).toBeDisabled();

  await userEvent.click(screen.getByRole("button", { name: "switch" }));
  // A fresh composer: empty draft, and SEND disabled only because the box is empty — typing
  // enables it, which a `busy` carried over from mission one would not.
  await userEvent.type(screen.getByTestId("composer-input"), "b");
  expect(screen.getByTestId("composer-send")).toBeEnabled();
});

test("an answer from a PREVIOUS visit is discarded even when you come back", async () => {
  // #896 review 11, finding 2. The fence was `isCurrent(asked)`, which is true again the moment
  // the operator returns — so m1 → m2 → m1 admitted a reply produced for a visit that is over
  // into a visit that is not.
  //
  // I argued the other way one round earlier, on the grounds that the turns live in the console
  // "so switching away and back does not discard an answer". The contract is the opposite and
  // this file already asserted it: moving away DELETES the pending turn. The id fence did not
  // preserve answers, it preserved the arbitrary subset whose operator happened to return in
  // time.
  //
  // Red against an id fence.
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.pulseAsk).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.pulseAsk>,
  );

  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "what happened?");
  await userEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(api.pulseAsk).toHaveBeenCalled());

  await userEvent.click(screen.getByText("switch"));
  await waitFor(() =>
    expect(screen.getByTestId("which")).toHaveTextContent("m2"),
  );
  await userEvent.click(screen.getByText("back"));
  await waitFor(() =>
    expect(screen.getByTestId("which")).toHaveTextContent("m1"),
  );

  release({ answer: "from a visit that is over", matches: [] });

  // The turn is GONE — the same thing navigating away has always done to it — rather than being
  // completed in a visit that did not ask for it.
  await waitFor(() =>
    expect(screen.getByTestId("count-m1")).toHaveTextContent("0"),
  );
  expect(screen.getByTestId("answers-m1")).toHaveTextContent("");
  expect(screen.getByTestId("count-m2")).toHaveTextContent("0");
});
