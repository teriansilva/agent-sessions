/** HQ's page (#1776): the two acceptance invariants of review 112556, each through the real
 *  consumers, and the route parsing.
 *
 *  1. **The shared list's generation guard.** A list read sent before a delete never restores the
 *     deleted conversation — on the page's list AND in the sidebar's switcher, which read one store.
 *  2. **The pending-turn Send lock.** A console never sends into a conversation whose turn it is not
 *     streaming (the hidden sidebar finishing one, another tab) until that turn settles.
 *
 *  The real-browser journeys (URL behaviour, reload, Back/Forward, login, Open as page, the in-flight
 *  handoff, touch targets) are `e2e/hq-page.spec.ts`. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import type { AppConfig, AskConversation, AskConversationSummary } from "../../types/api";

const config = vi.hoisted(() => ({ value: undefined as AppConfig | undefined }));
vi.mock("../../app/config", () => ({
  useConfig: () => config.value,
  useConfigRefresh: () => () => {},
}));
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      askConversations: vi.fn(),
      askConversation: vi.fn(),
      askNewConversation: vi.fn(),
      askDeleteConversation: vi.fn(),
      askMessageStream: vi.fn(),
      askSeen: vi.fn(),
    },
  };
});

import { api } from "../../lib/api";
import { AskConsole } from "../ask/AskConsole";
import {
  _resetForTests,
  conversationRemoved,
  refreshConversations,
} from "../ask/askConversationsStore";
import HqPage from "./HqPage";
import { hqTarget } from "./hqRoute";

const A = "ask_" + "a".repeat(32);
const B = "ask_" + "b".repeat(32);

function summary(id: string, title: string, updated: number): AskConversationSummary {
  return { id, title, created_at: 1, updated_at: updated };
}
const BOTH = { conversations: [summary(A, "alpha", 20), summary(B, "bravo", 10)], configured: true };
const ONLY_B = { conversations: [summary(B, "bravo", 10)], configured: true };

function conversation(id: string, extra: Partial<AskConversation> = {}): AskConversation {
  return {
    id,
    title: id === A ? "alpha" : "bravo",
    turns: [
      {
        turn_id: crypto.randomUUID(),
        question: id === A ? "alpha" : "bravo",
        asked_at: 1,
        status: "done",
        answer: `the ${id === A ? "alpha" : "bravo"} answer`,
        detail: null,
        code: null,
        matches: [],
        mission_matches: [],
        tools: [],
      },
    ],
    running: false,
    ...extra,
  } as AskConversation;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  _resetForTests();
  config.value = { pulse: { configured: true } } as AppConfig;
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
  vi.mocked(api.askSeen).mockResolvedValue({ unread: false });
  vi.mocked(api.askConversations).mockResolvedValue(BOTH);
  vi.mocked(api.askConversation).mockImplementation(async (id: string) => conversation(id));
});

// ---- the route --------------------------------------------------------------------------------

test("the route names the list, one well-formed conversation, or nothing", () => {
  expect(hqTarget("/hq")).toEqual({ kind: "list" });
  expect(hqTarget("/hq/")).toEqual({ kind: "list" });
  expect(hqTarget(`/hq/c/${A}`)).toEqual({ kind: "conversation", id: A });
  for (const bad of ["/hq/c/nope", "/hq/c/ask_ABC", `/hq/c/${A}/x`, "/hq/d/x", "/hq/c/%E0%A4%A"])
    expect(hqTarget(bad)).toEqual({ kind: "nothing" });
});

test("a malformed id is the same 'isn't here' as an unknown one, and asks the server nothing", async () => {
  render(
    <MemoryRouter initialEntries={["/hq/c/not-an-id"]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("hq-missing")).toHaveTextContent("isn't here");
  expect(api.askConversation).not.toHaveBeenCalled();
});

test("an id the server does not have renders the same state", async () => {
  const { ApiError } = await import("../../lib/api");
  vi.mocked(api.askConversation).mockRejectedValue(new ApiError(404, "no such conversation"));
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByTestId("hq-missing")).toHaveTextContent("isn't here");
});

// ---- invariant 1: the shared list's generation guard -----------------------------------------

test("the store: a read sent before a delete never restores the deleted conversation", async () => {
  await refreshConversations();
  const stale = deferred<typeof BOTH>();
  const after = deferred<typeof ONLY_B>();
  vi.mocked(api.askConversations)
    .mockReturnValueOnce(stale.promise)
    .mockReturnValueOnce(after.promise);
  const inFlight = refreshConversations(); // sent before the delete
  conversationRemoved(A);
  const fresh = refreshConversations(); // the delete's own refresh
  // The post-delete read answers first, then the stale one — the race the guard exists for.
  after.resolve(ONLY_B);
  await fresh;
  stale.resolve(BOTH);
  await inFlight;
  const { useAskConversations } = await import("../ask/askConversationsStore");
  function Rows() {
    const rows = useAskConversations();
    return <span data-testid="rows">{(rows ?? []).map((r) => r.id).join(",")}</span>;
  }
  render(<Rows />);
  expect(screen.getByTestId("rows")).toHaveTextContent(B);
  expect(screen.getByTestId("rows")).not.toHaveTextContent(A);
});

test("both consumers: deleting on the page never lets a stale read restore it on the page or in the sidebar", async () => {
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
      {/* The kept sidebar's console, mounted beside the page as the shell keeps it. */}
      <div data-testid="sidebar">
        <AskConsole configured visible={false} />
      </div>
    </MemoryRouter>,
  );
  const list = await screen.findByTestId("hq-list");
  await waitFor(() => expect(within(list).getAllByTestId("hq-list-item")).toHaveLength(2));
  expect(await screen.findByText("the alpha answer")).toBeInTheDocument();

  // A list read goes out (the sidebar's switcher opened) and hangs; the page then deletes A.
  const stale = deferred<typeof BOTH>();
  const after = deferred<typeof ONLY_B>();
  vi.mocked(api.askConversations)
    .mockReturnValueOnce(stale.promise)
    .mockReturnValueOnce(after.promise);
  const sidebar = screen.getByTestId("sidebar");
  await userEvent.click(within(sidebar).getByTestId("ask-switcher"));
  vi.mocked(api.askDeleteConversation).mockResolvedValue({ deleted: true, id: A } as never);
  const page = screen.getByTestId("hq-thread");
  await userEvent.click(within(page).getByTestId("ask-delete"));
  await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
  await waitFor(() => expect(api.askDeleteConversation).toHaveBeenCalledWith(A));

  // Gone at once from the page's list, before any read answers…
  await waitFor(() => expect(within(list).getAllByTestId("hq-list-item")).toHaveLength(1));
  // …and still gone after the post-delete read and then the stale one land.
  await act(async () => {
    after.resolve(ONLY_B);
    await Promise.resolve();
  });
  await act(async () => {
    stale.resolve(BOTH);
    await Promise.resolve();
  });
  expect(within(list).getAllByTestId("hq-list-item")).toHaveLength(1);
  expect(within(list).queryByText("alpha")).toBeNull();
  // The sidebar's switcher reads the SAME store: opened with its own read left hanging, it shows
  // exactly what the store holds now.
  vi.mocked(api.askConversations).mockReturnValueOnce(new Promise(() => {}));
  await userEvent.click(within(sidebar).getByTestId("ask-switcher"));
  const items = within(sidebar).getAllByTestId("ask-switcher-item");
  expect(items).toHaveLength(1);
  expect(items[0]).toHaveTextContent("bravo");
});

// ---- invariant 2: the pending-turn Send lock -------------------------------------------------

test("no console sends into a conversation with a turn it is not streaming, until that turn settles", async () => {
  // The hidden sidebar is still answering in A: the page reads it back PENDING.
  const done = conversation(A);
  const pending = { ...done, turns: [{ ...done.turns[0], status: "pending" as const, answer: null }] };
  let settled = false;
  vi.mocked(api.askConversation).mockImplementation(async (id: string) =>
    id === A ? (settled ? done : pending) : conversation(id),
  );
  vi.mocked(api.askMessageStream).mockImplementation(async (_id, body) => ({
    turn_id: body.turnId,
    question: body.text,
    asked_at: 2,
    status: "done",
    answer: "the follow-up answer",
    detail: null,
    code: null,
    matches: [],
    mission_matches: [],
    tools: [],
  }));
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const thread = await screen.findByTestId("hq-thread");
  await waitFor(() => expect(within(thread).getByTestId("ask-turn")).toBeInTheDocument());
  const input = within(thread).getByTestId("composer-input");
  await userEvent.type(input, "and then?{Enter}");
  const send = within(thread).getByRole("button", { name: /send/i });
  expect(send).toBeDisabled();
  expect(api.askMessageStream).not.toHaveBeenCalled();

  // The other console's turn settles; the page's poll reads it, and only then may it send.
  settled = true;
  await waitFor(() => expect(send).toBeEnabled(), { timeout: 5000 });
  await userEvent.click(send);
  await waitFor(() => expect(api.askMessageStream).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.askMessageStream).mock.calls[0][0]).toBe(A);
}, 15_000);

// ---- Hermes review 7393 ------------------------------------------------------------------------

function streamAnswers() {
  vi.mocked(api.askMessageStream).mockImplementation(async (_id, body) => ({
    turn_id: body.turnId,
    question: body.text,
    asked_at: 2,
    status: "done",
    answer: "sent answer",
    detail: null,
    code: null,
    matches: [],
    mission_matches: [],
    tools: [],
  }));
}

test("a deep link holds Send until the conversation's first read lands", async () => {
  streamAnswers();
  const held = deferred<AskConversation>();
  vi.mocked(api.askConversation).mockImplementation((id: string) =>
    id === A ? held.promise : Promise.resolve(conversation(id)),
  );
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const thread = await screen.findByTestId("hq-thread");
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(A));
  await userEvent.type(within(thread).getByTestId("composer-input"), "too early{Enter}");
  const send = within(thread).getByRole("button", { name: /send/i });
  expect(send).toBeDisabled();
  expect(api.askMessageStream).not.toHaveBeenCalled();
  await act(async () => held.resolve(conversation(A)));
  await waitFor(() => expect(send).toBeEnabled());
  await userEvent.click(send);
  await waitFor(() => expect(api.askMessageStream).toHaveBeenCalledTimes(1));
});

test("the kept sidebar reopening after the page was used holds Send until its re-read lands", async () => {
  streamAnswers();
  const { pageTouched } = await import("../ask/askConversationsStore");
  const { useState } = await import("react");
  let show: (v: boolean) => void = () => {};
  function Kept() {
    const [visible, setVisible] = useState(true);
    show = setVisible;
    return <AskConsole configured visible={visible} />;
  }
  render(
    <MemoryRouter>
      <Kept />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByTestId("ask-switcher"));
  await userEvent.click((await screen.findAllByTestId("ask-switcher-item"))[0]);
  expect(await screen.findByText("the alpha answer")).toBeInTheDocument();
  act(() => show(false));
  pageTouched(); // the page was used while the panel was hidden
  const held = deferred<AskConversation>();
  vi.mocked(api.askConversation).mockReturnValueOnce(held.promise);
  act(() => show(true));
  await userEvent.type(screen.getByTestId("composer-input"), "after reopening{Enter}");
  const send = screen.getByRole("button", { name: /send/i });
  expect(send).toBeDisabled();
  expect(api.askMessageStream).not.toHaveBeenCalled();
  await act(async () => held.resolve(conversation(A)));
  await waitFor(() => expect(send).toBeEnabled());
});

test("unsent text stays with its conversation: a pick, New and coming back never carry it across", async () => {
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const thread = await screen.findByTestId("hq-thread");
  expect(await within(thread).findByText("the alpha answer")).toBeInTheDocument();
  const input = () => within(thread).getByTestId("composer-input");
  await userEvent.type(input(), "private draft for alpha");

  const list = screen.getByTestId("hq-list");
  await userEvent.click(within(list).getByText("bravo"));
  expect(await within(thread).findByText("the bravo answer")).toBeInTheDocument();
  expect(input()).toHaveValue(""); // never A's words in B's box
  await userEvent.type(input(), "bravo's own");

  await userEvent.click(within(list).getByTestId("hq-new"));
  await waitFor(() => expect(within(thread).queryByText("the bravo answer")).toBeNull());
  expect(input()).toHaveValue("");

  await userEvent.click(within(list).getByText("alpha"));
  expect(await within(thread).findByText("the alpha answer")).toBeInTheDocument();
  expect(input()).toHaveValue("private draft for alpha"); // kept where it was typed
  await userEvent.click(within(list).getByText("bravo"));
  expect(await within(thread).findByText("the bravo answer")).toBeInTheDocument();
  expect(input()).toHaveValue("bravo's own");
});


test("A → B → A: Send waits for the returning A's own read, not its first one (review 7399)", async () => {
  streamAnswers();
  render(
    <MemoryRouter initialEntries={[`/hq/c/${A}`]}>
      <Routes>
        <Route path="/hq/*" element={<HqPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const thread = await screen.findByTestId("hq-thread");
  expect(await within(thread).findByText("the alpha answer")).toBeInTheDocument();
  // B's read and the returning A's read are both held.
  const heldB = deferred<AskConversation>();
  const heldA = deferred<AskConversation>();
  vi.mocked(api.askConversation).mockImplementation((id: string) =>
    id === B ? heldB.promise : heldA.promise,
  );
  const list = screen.getByTestId("hq-list");
  await userEvent.click(within(list).getByText("bravo"));
  await waitFor(() => expect(api.askConversation).toHaveBeenCalledWith(B));
  await userEvent.click(within(list).getByText("alpha"));
  await waitFor(() => expect(vi.mocked(api.askConversation).mock.calls.at(-1)?.[0]).toBe(A));
  await userEvent.type(within(thread).getByTestId("composer-input"), "back in alpha{Enter}");
  const send = within(thread).getByRole("button", { name: /send/i });
  expect(send).toBeDisabled();
  expect(api.askMessageStream).not.toHaveBeenCalled();
  // B's late read lands for a conversation no longer on screen: still nothing.
  await act(async () => heldB.resolve(conversation(B)));
  expect(send).toBeDisabled();
  await act(async () => heldA.resolve(conversation(A)));
  await waitFor(() => expect(send).toBeEnabled());
});
