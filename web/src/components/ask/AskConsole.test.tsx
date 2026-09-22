/** ASK (#878, its own page since #1058) — the box's behaviour, plus the lifecycle rules that only
 *  exist because the answers are transient.
 *
 *  Five of these descend from `Composer.test.tsx`, which descended from `Pulse.test.tsx`'s Ask cases
 *  (#522): the answer renders, a 409 surfaces the server's own detail rather than a generic error, a
 *  follow-up replays the prior turns as history, an unconfigured endpoint disables the control and
 *  makes no call, and the page says the answers are not kept.
 *
 *  **What changed with the move, and what it means for these tests.** The composer's discard rule
 *  needed a `visit()` token compared at resolution time, because switching missions or flipping
 *  Active → Archived did not necessarily unmount the box — an answer could arrive into a surface
 *  that was still on screen but was no longer the one that asked. On a route that cannot happen:
 *  leaving unmounts the page and the turns are its own state. So the "discarded, not filed" case
 *  below asserts the OBSERVABLE consequence (coming back finds nothing, and the late resolution
 *  neither throws nor resurrects a turn) and says openly that the unmount is what makes it hold —
 *  rather than pretending a ref is doing work that React's own lifecycle does.
 *
 *  The ref DOES do one job, and the last test is the one that is red without it: under StrictMode an
 *  effect is cleaned up and re-run on the same fiber, and a `useRef(true)` initialises once — so a
 *  cleanup-only liveness flag stays false for the rest of the component's life and drops every
 *  answer in development. That is a real regression with a real red test, which is the only kind
 *  worth writing here.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../lib/api";

import { AskConsole } from "./AskConsole";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { pulseAsk: vi.fn() },
  };
});

/** The console needs a router: a match row renders a `<Link>` into the session. */
function mount(ui: React.ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

/** The page's own mount/unmount, driven from a control — the route transition these tests are
 *  about, without pulling the whole router in. */
function Host() {
  const [open, setOpen] = useState(true);
  return (
    <MemoryRouter>
      <button type="button" onClick={() => setOpen(false)}>
        leave
      </button>
      <button type="button" onClick={() => setOpen(true)}>
        return
      </button>
      {open ? <AskConsole configured /> : <div data-testid="gone" />}
    </MemoryRouter>
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
  mount(<AskConsole configured />);
  await userEvent.type(
    screen.getByTestId("composer-input"),
    "when did I merge it",
  );
  await userEvent.click(screen.getByTestId("composer-send"));
  const turns = await screen.findByTestId("ask-turns");
  expect(
    within(turns).getByText("You merged it on Tuesday."),
  ).toBeInTheDocument();
});

test("a matched session is named, explained and reachable (#522)", async () => {
  // An answer that names a session the operator cannot open is half an answer — the match row
  // carries the reason it matched AND the way in.
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "Two sessions touched it.",
    matches: [
      { id: "claude:abc-123", title: "fix flaky upload retry", why: "uploads.py" },
    ],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "upload retry");
  await userEvent.click(screen.getByTestId("composer-send"));
  const row = await screen.findByTestId("ask-match");
  expect(within(row).getByText("uploads.py")).toBeInTheDocument();
  expect(
    within(row).getByRole("link", { name: "Jump into fix flaky upload retry" }),
  ).toHaveAttribute("href", "/s/claude/abc-123");
});

test("a busy 409 surfaces the server's detail, not a generic error (#522)", async () => {
  // The detail IS the answer here — "a question is already running" tells the operator to wait,
  // where "that didn't work" tells them to retry, which is the wrong move.
  vi.mocked(api.pulseAsk).mockRejectedValue(
    new ApiError(409, "a question is already running"),
  );
  mount(<AskConsole configured />);
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
  mount(<AskConsole configured />);
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
  mount(<AskConsole configured={false} />);
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(api.pulseAsk).not.toHaveBeenCalled();
});

test("the page says these answers are not kept (#878)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "ok",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  mount(<AskConsole configured />);
  await userEvent.type(screen.getByTestId("composer-input"), "x");
  await userEvent.click(screen.getByTestId("composer-send"));
  // The operator is told, rather than discovering it by reloading and finding nothing. The
  // wording says PAGE now, not view — #1058 moved the surface, and the copy moved with it.
  const note = await screen.findByTestId("ask-transient");
  expect(note).toHaveTextContent(/not kept|disappear/i);
  expect(note).toHaveTextContent(/page/i);
});

test("a reply that lands AFTER leaving is DISCARDED, and returning finds nothing", async () => {
  // #878's contract on a route. The turns are this component's state, so leaving deletes them —
  // that IS the mechanism, and this asserts its consequences: the late resolution does not throw,
  // does not resurrect the pending turn, and a fresh visit starts empty rather than replaying an
  // answer the operator never waited for.
  let resolveIt: ((v: unknown) => void) | undefined;
  vi.mocked(api.pulseAsk).mockReturnValue(
    new Promise((res) => {
      resolveIt = res;
    }) as ReturnType<typeof api.pulseAsk>,
  );

  render(<Host />);
  await userEvent.type(screen.getByTestId("composer-input"), "for this visit");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(screen.getAllByTestId("ask-turn")).toHaveLength(1);

  // …the operator leaves before the answer comes back.
  await userEvent.click(screen.getByRole("button", { name: "leave" }));
  expect(screen.getByTestId("gone")).toBeInTheDocument();

  resolveIt?.({
    answer: "An answer nobody is waiting for.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  await Promise.resolve();

  await userEvent.click(screen.getByRole("button", { name: "return" }));
  // A fresh page: no turns at all, and certainly not that answer.
  expect(screen.queryByTestId("ask-turns")).not.toBeInTheDocument();
  expect(
    screen.queryByText("An answer nobody is waiting for."),
  ).not.toBeInTheDocument();
  // …and the box is usable, not stuck busy from a request the previous page made.
  await userEvent.type(screen.getByTestId("composer-input"), "b");
  expect(screen.getByTestId("composer-send")).toBeEnabled();
});

test("StrictMode's effect re-run does not make the page drop every answer", async () => {
  // RED without `live.current = true` in the effect BODY. StrictMode cleans an effect up and runs
  // it again on the same fiber, and `useRef(true)` initialises exactly once — so a cleanup-only
  // liveness flag is false from the first re-run onward and every `then` returns early. The page
  // renders, accepts typing, calls the API, and then silently never answers: the worst shape a
  // regression can take, because nothing errors.
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "It still answers.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  render(
    <StrictMode>
      <MemoryRouter>
        <AskConsole configured />
      </MemoryRouter>
    </StrictMode>,
  );
  await userEvent.type(screen.getByTestId("composer-input"), "anything");
  await userEvent.click(screen.getByTestId("composer-send"));
  expect(
    await within(await screen.findByTestId("ask-turns")).findByText(
      "It still answers.",
    ),
  ).toBeInTheDocument();
});
