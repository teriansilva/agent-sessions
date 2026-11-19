import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

// What a paired instance suggests (#2061): what the cards ask the server for and what they show
// of the answers. Real layout, plain-text rendering in a browser and the phone are the browser
// spec's (e2e/peer-input.spec.ts).
const api = vi.hoisted(() => ({
  federationSuggestions: vi.fn(),
  federationGrants: vi.fn(),
  approveFederationSuggestion: vi.fn(),
  rejectFederationSuggestion: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { ApiError } = await import("../../lib/api");
const { PeerSuggestions } = await import("./PeerSuggestions");
const { PEER_SUGGESTION_EVENT } = await import("../../lib/federationInput");

const KEY = "codex-api:0d0f6a52-1111-4222-8333-444455556666";
const A = "sAbCdEfGhIjKlMnOpQrStU";
const B = "tAbCdEfGhIjKlMnOpQrStU";
const suggestion = (over = {}) => ({
  suggestion_id: A,
  grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
  session_key: KEY,
  peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
  peer_name: "gpu-box",
  actor: "",
  text: "Please keep the old batch entry point.",
  state: "pending",
  approved_text: null,
  approved_by: null,
  approved_at: null,
  edited: false,
  handoff: null,
  end_reason: null,
  created_at: 100,
  updated_at: 100,
  expires_at: 9e9,
  ...over,
});
const listed = (...rows: unknown[]) => ({ suggestions: rows });
const cards = () => screen.queryAllByTestId("peer-suggestion");

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.federationGrants.mockResolvedValue({ grants: [] });
  api.federationSuggestions.mockResolvedValue(listed());
});

test("nothing waiting: nothing is drawn", async () => {
  const { container } = render(<PeerSuggestions sessionKey={KEY} />);
  await waitFor(() => expect(api.federationSuggestions).toHaveBeenCalledWith(KEY));
  expect(container).toBeEmptyDOMElement();
});

test("a card: the peer, 'suggests', 'waiting', the text, three buttons, the line", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  render(<PeerSuggestions sessionKey={KEY} />);
  const card = await screen.findByTestId("peer-suggestion");
  expect(card).toHaveAttribute("data-state", "pending");
  expect(within(card).getByTestId("peer-badge")).toHaveTextContent("gpu-box");
  expect(within(card).getByTestId("peer-suggestion-word")).toHaveTextContent("waiting");
  expect(within(card).getByTestId("peer-suggestion-text")).toHaveTextContent("Please keep the old batch entry point.");
  expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual(["Approve", "Edit", "Reject"]);
  expect(within(card).getByTestId("peer-suggestion-note")).toHaveTextContent(
    "Nothing is sent until you approve. It is not shown to the agent.",
  );
  expect(screen.queryByTestId("peer-suggestions-count")).toBeNull(); // one needs no count
  // nothing on the card takes focus by itself: a key press approves nothing
  expect(document.activeElement).toBe(document.body);
});

test("Approve calls the approve route once, for that suggestion, without text", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion(), suggestion({ suggestion_id: B, created_at: 200 })));
  api.approveFederationSuggestion.mockResolvedValue({ suggestion: suggestion({ state: "approved", handoff: "sent" }) });
  render(<PeerSuggestions sessionKey={KEY} />);
  await waitFor(() => expect(cards()).toHaveLength(2));
  expect(screen.getByTestId("peer-suggestions-count")).toHaveTextContent("2 waiting");
  expect(screen.getAllByTestId("peer-suggestion-note")).toHaveLength(1); // under the first card
  await userEvent.click(within(cards()[0]).getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(cards()).toHaveLength(1));
  expect(api.approveFederationSuggestion).toHaveBeenCalledTimes(1);
  expect(api.approveFederationSuggestion).toHaveBeenCalledWith(A, undefined);
  expect(api.rejectFederationSuggestion).not.toHaveBeenCalled();
  expect(cards()[0]).toHaveTextContent("Please keep"); // the other one is still waiting
  expect(within(cards()[0]).getByTestId("peer-suggestion-word")).toHaveTextContent("waiting");
});

test("Edit, then Approve edited sends the changed text; Cancel restores the card", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  api.approveFederationSuggestion.mockResolvedValue({ suggestion: suggestion({ state: "approved", edited: true }) });
  render(<PeerSuggestions sessionKey={KEY} />);
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const box = screen.getByTestId("peer-suggestion-edit");
  expect(box).toHaveValue("Please keep the old batch entry point.");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByTestId("peer-suggestion-text")).toHaveTextContent("Please keep the old batch entry point.");
  await userEvent.click(screen.getByRole("button", { name: "Edit" }));
  await userEvent.clear(screen.getByTestId("peer-suggestion-edit"));
  expect(screen.getByRole("button", { name: "Approve edited" })).toBeDisabled(); // nothing to approve
  await userEvent.type(screen.getByTestId("peer-suggestion-edit"), "Keep it for one release.{Enter}");
  expect(api.approveFederationSuggestion).not.toHaveBeenCalled(); // Enter is a newline, not a decision
  await userEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(api.approveFederationSuggestion).toHaveBeenCalledWith(A, "Keep it for one release.\n"),
  );
  await waitFor(() => expect(cards()).toHaveLength(0));
});

test("Reject calls the reject route once", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  api.rejectFederationSuggestion.mockResolvedValue({ suggestion: suggestion({ state: "rejected" }) });
  render(<PeerSuggestions sessionKey={KEY} />);
  await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
  await waitFor(() => expect(cards()).toHaveLength(0));
  expect(api.rejectFederationSuggestion).toHaveBeenCalledTimes(1);
  expect(api.rejectFederationSuggestion).toHaveBeenCalledWith(A);
  expect(api.approveFederationSuggestion).not.toHaveBeenCalled();
});

test("approving: decided, not delivered. No buttons, and never the word delivered", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  api.approveFederationSuggestion.mockResolvedValue({ suggestion: suggestion({ state: "approving" }) });
  render(<PeerSuggestions sessionKey={KEY} />);
  const approve = await screen.findByRole("button", { name: "Approve" });
  api.federationSuggestions.mockResolvedValue(listed(suggestion({ state: "approving" })));
  await userEvent.click(approve);
  const card = await screen.findByTestId("peer-suggestion");
  await waitFor(() => expect(card).toHaveAttribute("data-state", "approving"));
  expect(within(card).getByTestId("peer-suggestion-approving")).toHaveTextContent("Approved · delivering…");
  expect(within(card).queryAllByRole("button")).toHaveLength(0);
  expect(card).not.toHaveTextContent(/delivered|sent/i);
});

test.each([
  [{ code: "conflict", state: "withdrawn" }, suggestion({ state: "withdrawn", end_reason: "peer" }), "gpu-box withdrew this suggestion."],
  [{ code: "conflict", state: "withdrawn" }, suggestion({ state: "withdrawn", end_reason: "grant_revoked" }), "This suggestion was withdrawn because the share ended."],
  [{ code: "ended", state: "pending" }, suggestion(), "This suggestion was withdrawn because the share ended."],
  [{ code: "conflict", state: "approved" }, suggestion({ state: "approved" }), "Already approved."],
  [{ code: "conflict", state: "expired" }, suggestion({ state: "expired" }), "This suggestion expired."],
])("a 409 shows what happened (%o)", async (body, now, text) => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  api.approveFederationSuggestion.mockRejectedValue(new ApiError(409, "already decided", body));
  render(<PeerSuggestions sessionKey={KEY} />);
  const approve = await screen.findByRole("button", { name: "Approve" });
  api.federationSuggestions.mockResolvedValue(listed(now));
  await userEvent.click(approve);
  expect(await screen.findByTestId("peer-suggestion-outcome")).toHaveTextContent(text);
  expect(within(screen.getByTestId("peer-suggestion")).queryAllByRole("button")).toHaveLength(0);
});

test("a mission holds the session: nothing was decided, the card stays and says why", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  api.approveFederationSuggestion.mockRejectedValue(new ApiError(409, "held", { code: "held" }));
  render(<PeerSuggestions sessionKey={KEY} />);
  await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
  expect(await screen.findByTestId("peer-suggestion-error")).toHaveTextContent(/belongs to a mission right now/);
  expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
});

test("the bell's event reads the list at once", async () => {
  render(<PeerSuggestions sessionKey={KEY} />);
  await waitFor(() => expect(api.federationSuggestions).toHaveBeenCalledTimes(1));
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  act(() => void window.dispatchEvent(new Event(PEER_SUGGESTION_EVENT)));
  expect(await screen.findByTestId("peer-suggestion")).toBeInTheDocument();
});

test("another session in the pane: the old cards are gone before the new list arrives", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion()));
  const { rerender } = render(<PeerSuggestions sessionKey={KEY} />);
  await screen.findByTestId("peer-suggestion");
  api.federationSuggestions.mockReturnValue(new Promise(() => {}));
  rerender(<PeerSuggestions sessionKey="codex-api:other" />);
  expect(cards()).toHaveLength(0);
});

// ---- StrictMode (main.tsx mounts the app under it) ---------------------------------------------

test("under StrictMode the cards load, the grants start the poll, and a decision's answer is shown", async () => {
  api.federationSuggestions.mockResolvedValue(listed(suggestion(), suggestion({ suggestion_id: B, created_at: 200 })));
  api.federationGrants.mockResolvedValue({ grants: [{ level: "suggest" }] });
  api.approveFederationSuggestion.mockResolvedValue({ suggestion: suggestion({ state: "approved", handoff: "sent" }) });
  render(
    <StrictMode>
      <PeerSuggestions sessionKey={KEY} />
    </StrictMode>,
  );
  await waitFor(() => expect(cards()).toHaveLength(2));
  expect(screen.getByTestId("peer-suggestions-count")).toHaveTextContent("2 waiting");
  await userEvent.click(within(cards()[0]).getByRole("button", { name: "Approve" }));
  // the answer of the replayed mount's fence is applied: the approved card leaves
  await waitFor(() => expect(cards()).toHaveLength(1));
  expect(api.approveFederationSuggestion).toHaveBeenCalledTimes(1);
  expect(within(cards()[0]).getByRole("button", { name: "Approve" })).toBeEnabled(); // not stuck busy
});

test("under StrictMode a late list for the session shown before is dropped", async () => {
  let late: (v: unknown) => void = () => {};
  api.federationSuggestions.mockImplementation((key: string) =>
    key === KEY ? new Promise((resolve) => (late = resolve)) : Promise.resolve(listed()),
  );
  const { rerender } = render(
    <StrictMode>
      <PeerSuggestions sessionKey={KEY} />
    </StrictMode>,
  );
  await waitFor(() => expect(api.federationSuggestions).toHaveBeenCalledWith(KEY));
  rerender(
    <StrictMode>
      <PeerSuggestions sessionKey="codex-api:other" />
    </StrictMode>,
  );
  await waitFor(() => expect(api.federationSuggestions).toHaveBeenCalledWith("codex-api:other"));
  await act(async () => late(listed(suggestion())));
  expect(cards()).toHaveLength(0);
});

// ---- a peer's numbers ---------------------------------------------------------------------------

test("times no Date can hold never throw and never drop a card", async () => {
  const hostile: unknown[] = [1e300, Number.MAX_VALUE, Infinity, -Infinity, NaN, -1, "soon", null];
  api.federationSuggestions.mockResolvedValue(
    listed(
      ...hostile.map((v, i) =>
        suggestion({
          suggestion_id: `h${i}`.padEnd(22, "x"),
          created_at: v,
          updated_at: v,
          expires_at: v,
          approved_at: v,
          text: `hostile ${i}`,
        }),
      ),
    ),
  );
  render(<PeerSuggestions sessionKey={KEY} />);
  await waitFor(() => expect(cards()).toHaveLength(hostile.length));
  expect(screen.getByTestId("peer-suggestions-count")).toHaveTextContent(`${hostile.length} waiting`);
  for (let i = 0; i < hostile.length; i++) expect(screen.getByText(`hostile ${i}`)).toBeInTheDocument();
});
