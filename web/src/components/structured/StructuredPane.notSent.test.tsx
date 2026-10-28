import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot, StructuredTurn } from "../../types/api";
import {
  NOT_SENT_TEXT,
  canSendAgain,
  missingPictures,
  missingPrompt,
  notSentDiscarded,
  notSentState,
  notSentText,
} from "./notSent";
import { StructuredPane } from "./StructuredPane";

/** #1538: a queued message proven not sent says why and offers Send again — a NEW operation with
 *  the same words and attachments, pending at once through the outbox. */

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      structuredDecide: vi.fn(),
      structuredInterrupt: vi.fn(),
      structuredStop: vi.fn(),
      upload: vi.fn(),
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      uploadBlob: vi.fn(),
      session: vi.fn(() => new Promise(() => {})),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `claude-api:${ID}`;
const PIC = "20261020-114424-shot.png";

function turn(over: Partial<StructuredTurn> = {}): StructuredTurn {
  return {
    turn_id: crypto.randomUUID(),
    operation_id: "",
    state: "completed",
    text: "hello",
    reply: "echo:hello",
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    ...over,
  };
}

/** What the server derives for Send again (`native_journal.retry_id`); any fixed id will do here. */
const RETRY = "9d4b6c2a-3f1e-5b7d-8a90-1c2d3e4f5a6b";

function notSent(over: Partial<StructuredTurn> = {}): StructuredTurn {
  const id = crypto.randomUUID();
  return turn({
    turn_id: id,
    operation_id: id,
    state: "failed",
    text: "Will this also improve? What leads to this?",
    reply: "",
    reason: "not sent: the session ended while this message waited — nothing was delivered",
    not_sent: { code: "worker_ended", retry: RETRY, retried: false },
    ...over,
  });
}

function snap(turns: StructuredTurn[], over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns,
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function renderPane() {
  return render(
    <MemoryRouter>
      <StructuredPane engine="claude-api" id={ID} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.structuredEvents).mockResolvedValue({ session_key: KEY, revision: 4, next_cursor: 4, events: [] });
  vi.mocked(api.uploadBlob).mockResolvedValue(new Blob(["x"]));
});

afterEach(() => {
  vi.clearAllMocks();
  resetRoster();
});

test("the reason is fixed text from the code, never the server's words", async () => {
  const t = notSent({ not_sent: { code: "agent_changed", retry: RETRY, retried: false }, reason: "not sent: /secret/path from the server" });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  expect(row).toHaveTextContent(`Not sent ${NOT_SENT_TEXT.agent_changed}`);
  expect(row).not.toHaveTextContent("/secret/path");
  expect(screen.queryByTestId("structured-failed")).toBeNull();
  expect(within(row).getByRole("button", { name: "Send again" })).toBeEnabled();
  expect(within(row).getByRole("button", { name: "Discard" })).toBeEnabled();
});

test("Send again posts the server-derived retry id with the same text and attachments, pending at once", async () => {
  const user = userEvent.setup();
  const t = notSent({ attachments: [{ stored: PIC, name: "shot.png", mime: "image/png" }] });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  vi.mocked(api.structuredSubmit).mockImplementation(() => new Promise(() => {}));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  await user.click(within(row).getByRole("button", { name: "Send again" }));
  // Pending at once, in the outbox, before any answer came back.
  const pending = await screen.findByTestId("structured-pending");
  expect(pending).toHaveTextContent("Will this also improve? What leads to this?");
  expect(within(pending).getByTestId("structured-pending-attachment")).toHaveTextContent("shot.png");
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(1));
  const [key, operation, text, , attachments] = vi.mocked(api.structuredSubmit).mock.calls[0];
  expect(key).toBe(KEY);
  expect(operation).toBe(RETRY); // the server's derived id, never a fresh one
  expect(text).toBe(t.text);
  expect(attachments).toEqual([PIC]);
  expect(api.uploadBlob).toHaveBeenCalledWith(PIC);
  // While it is on its way the original says so instead of offering the button.
  const sending = screen.getByTestId("structured-not-sent");
  expect(sending).toHaveAttribute("data-settled", "sending");
  expect(sending).toHaveTextContent("Sending again…");
  expect(within(sending).queryByRole("button")).toBeNull();
});

test("a retry the server holds is history on every device: read from the snapshot, not storage", async () => {
  const t = notSent({ not_sent: { code: "worker_ended", retry: RETRY, retried: true } });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  expect(row).toHaveAttribute("data-settled", "again");
  expect(row).toHaveTextContent("Sent again below.");
  expect(within(row).queryByRole("button")).toBeNull();
  expect(notSentDiscarded(KEY, t.turn_id)).toBe(false); // nothing on this device says so
});

test("a retry the server never recorded leaves Send again after a reload", async () => {
  const user = userEvent.setup();
  const t = notSent();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  vi.mocked(api.structuredSubmit).mockImplementation(() => new Promise(() => {}));
  const first = renderPane();
  await user.click(within(await screen.findByTestId("structured-not-sent")).getByRole("button", { name: "Send again" }));
  await screen.findByTestId("structured-not-sent-sending");
  first.unmount(); // the reload: the tab's outbox is gone, the server never recorded it
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  expect(within(row).getByRole("button", { name: "Send again" })).toBeEnabled();
});

test("a refused retry offers Send again again, under the same id", async () => {
  const user = userEvent.setup();
  const t = notSent();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  vi.mocked(api.structuredSubmit).mockRejectedValueOnce(new ApiError(409, "the session is read only"));
  renderPane();
  await user.click(within(await screen.findByTestId("structured-not-sent")).getByRole("button", { name: "Send again" }));
  const refused = await screen.findByText(/Not sent: the session is read only/);
  expect(refused).toBeInTheDocument();
  const row = screen.getByTestId("structured-not-sent");
  vi.mocked(api.structuredSubmit).mockImplementation(() => new Promise(() => {}));
  await user.click(within(row).getByRole("button", { name: "Send again" }));
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.structuredSubmit).mock.calls.map((c) => c[1])).toEqual([RETRY, RETRY]);
  // The refused entry was replaced, not duplicated.
  expect(screen.getAllByTestId("structured-pending")).toHaveLength(1);
});

test("Discard leaves the row as history without sending anything", async () => {
  const user = userEvent.setup();
  const t = notSent();
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  await user.click(within(row).getByRole("button", { name: "Discard" }));
  expect(screen.getByTestId("structured-not-sent")).toHaveAttribute("data-settled", "discarded");
  expect(within(screen.getByTestId("structured-not-sent")).queryByRole("button")).toBeNull();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
  expect(notSentDiscarded(KEY, t.turn_id)).toBe(true);
});

test("a missing picture is asked about, and Send without it sends the rest", async () => {
  const user = userEvent.setup();
  const doc = { stored: "20261020-114424-notes.txt", name: "notes.txt", mime: "application/octet-stream" };
  const t = notSent({ attachments: [{ stored: PIC, name: "shot.png", mime: "image/png" }, doc] });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  vi.mocked(api.uploadBlob).mockRejectedValue(new ApiError(404, "upload read-back → 404"));
  vi.mocked(api.structuredSubmit).mockImplementation(() => new Promise(() => {}));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  await user.click(within(row).getByRole("button", { name: "Send again" }));
  const prompt = await screen.findByTestId("structured-not-sent-missing");
  expect(prompt).toHaveTextContent("A picture is no longer available — send without it?");
  expect(api.structuredSubmit).not.toHaveBeenCalled();
  expect(screen.queryByTestId("structured-pending")).toBeNull();
  await user.click(within(prompt).getByRole("button", { name: "Send without it" }));
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][4]).toEqual([doc.stored]);
});

test("Cancel on the missing-picture prompt sends nothing and offers Send again again", async () => {
  const user = userEvent.setup();
  const t = notSent({ attachments: [{ stored: PIC, name: "shot.png", mime: "image/png" }] });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  vi.mocked(api.uploadBlob).mockRejectedValue(new ApiError(404, "gone"));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  await user.click(within(row).getByRole("button", { name: "Send again" }));
  const prompt = await screen.findByTestId("structured-not-sent-missing");
  await user.click(within(prompt).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByTestId("structured-not-sent-missing")).toBeNull();
  expect(within(row).getByRole("button", { name: "Send again" })).toBeEnabled();
  expect(api.structuredSubmit).not.toHaveBeenCalled();
});

test("no Send again on a delivered, uncertain or Send-now-refused turn, or a visitor's", async () => {
  const turns = [
    turn({ text: "delivered" }),
    turn({ state: "uncertain", text: "uncertain", reply: "", reason: "the native worker ended before this turn settled" }),
    // A direct submit the agent refused: failed, but no `not_sent` (it never waited in a queue).
    // The agent refused it AFTER it was handed off (no `not_sent`): nothing proves it unsent.
    turn({ state: "failed", text: "refused", reply: "", reason: "the agent refused this turn" }),
    // A queued message whose Send now control was refused stays queued: nothing to resend.
    turn({ state: "queued", text: "still queued", reply: "", delivery: "interrupt_failed", delivery_reason: "refused" }),
    notSent({ text: "visitor", author: { name: "Ann", via: "public_link" } }),
  ];
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap(turns));
  renderPane();
  await screen.findByText("delivered");
  expect(screen.queryByRole("button", { name: "Send again" })).toBeNull();
  // The visitor's message still says why, but is not resent as the operator's.
  expect(screen.getByTestId("structured-not-sent")).toHaveTextContent(NOT_SENT_TEXT.worker_ended);
});

test("a read-only session shows the reason without actions", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([notSent()], { read_only: "the client was removed" }));
  renderPane();
  const row = await screen.findByTestId("structured-not-sent");
  expect(row).toHaveTextContent(NOT_SENT_TEXT.worker_ended);
  expect(within(row).queryByRole("button")).toBeNull();
});

test("the code table is closed: an unknown code reads as unexpected", () => {
  expect(notSentText("worker_ended")).toBe(NOT_SENT_TEXT.worker_ended);
  expect(notSentText("toString")).toBe(NOT_SENT_TEXT.unexpected);
  expect(notSentText("<b>raw</b>")).toBe(NOT_SENT_TEXT.unexpected);
  expect(Object.keys(NOT_SENT_TEXT).sort()).toEqual(
    ["agent_changed", "agent_removed", "generation_closed", "journal_unwritable", "question_replaced", "source_store_changed", "unexpected", "worker_ended"],
  );
});

test("the row's state: the server first, then this tab's outbox, then Discard", () => {
  const t = notSent();
  expect(notSentState(t, null, false)).toBeNull();
  expect(notSentState(t, "waiting", false)).toBe("sending");
  expect(notSentState(t, "unknown", false)).toBe("sending");
  expect(notSentState(t, "refused", false)).toBeNull();
  expect(notSentState(t, null, true)).toBe("discarded");
  expect(notSentState(notSent({ not_sent: { code: "unexpected", retry: RETRY, retried: true } }), null, true)).toBe("again");
});

test("canSendAgain needs not_sent with its retry id, the operator's own words, and the whole text", () => {
  expect(canSendAgain(notSent())).toBe(true);
  expect(canSendAgain(notSent({ not_sent: { code: "unexpected" } }))).toBe(false);
  expect(canSendAgain(notSent({ not_sent: { code: "unexpected", retry: RETRY, retried: true } }))).toBe(false);
  expect(canSendAgain(notSent({ not_sent: { code: "unexpected", retry: RETRY, retry_recorded: true } }))).toBe(false);
  expect(canSendAgain(notSent({ not_sent: undefined }))).toBe(false);
  expect(canSendAgain(notSent({ text_truncated: true }))).toBe(false);
  expect(canSendAgain(notSent({ author: { name: null, via: "unknown" } }))).toBe(false);
});

test("only a 404 proves a picture missing; files are never probed", async () => {
  const probe = vi.fn(async (stored: string) => {
    if (stored === "gone.png") throw new ApiError(404, "gone");
    if (stored === "flaky.png") throw new ApiError(503, "busy");
  });
  const gone = await missingPictures(
    [
      { stored: "gone.png", mime: "image/png" },
      { stored: "flaky.png", mime: "image/png" },
      { stored: "notes.txt", mime: "application/octet-stream" },
    ],
    probe,
  );
  expect(gone.map((a) => a.stored)).toEqual(["gone.png"]);
  expect(probe).not.toHaveBeenCalledWith("notes.txt");
  expect(missingPrompt(2, false)).toBe("2 pictures are no longer available — send without them?");
  expect(missingPrompt(1, true)).toBe("A picture is no longer available, and nothing else is left to send.");
});

test("a retry refused before stdin chains: the original is history, the retry row offers the next attempt", async () => {
  const user = userEvent.setup();
  const SECOND = "4c7d2e1f-8a9b-5c0d-9e1f-2a3b4c5d6e7f";
  const original = notSent({ not_sent: { code: "worker_ended", retry: RETRY, retry_recorded: true, retried: false } });
  const retry = notSent({
    turn_id: RETRY,
    operation_id: RETRY,
    not_sent: { code: "unexpected", retry: SECOND, retry_recorded: false, retried: false },
  });
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([original, retry]));
  vi.mocked(api.structuredSubmit).mockImplementation(() => new Promise(() => {}));
  renderPane();
  const rows = await screen.findAllByTestId("structured-not-sent");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveAttribute("data-settled", "again");
  expect(within(rows[0]).queryByRole("button")).toBeNull();
  expect(within(rows[1]).getByRole("button", { name: "Send again" })).toBeEnabled();
  expect(screen.getAllByRole("button", { name: "Send again" })).toHaveLength(1); // never twice
  await user.click(within(rows[1]).getByRole("button", { name: "Send again" }));
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][1]).toBe(SECOND);
});
