/** The relay composer's settlement fence (#983 P3, review 4887 finding 3).
 *
 * Edit can hand this composer a new draft while a send is still in flight, so a response belongs to
 * the send it came from and to nothing else. What it may still do is announce its own DURABLE
 * outcome — the server closed that draft, so the card must go. What it may NOT do is touch the
 * composer any more: clearing the text would erase what the operator just adopted, and clearing the
 * replacement would turn the next Send into an ordinary relay that leaves a live proposal behind.
 *
 * Driven through the real component with genuinely deferred promises, because the bug is entirely
 * about ORDERING: a stub that resolves inline has no window to reproduce.
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { DraftEdit } from "./draftDirection";

import { MissionScreen } from "./MissionScreen";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { relayToSession: vi.fn(), missionScreen: vi.fn() } };
});

const MISSION = "msn_1";
const SESSION = "claude:aaa";
const A: DraftEdit = { actionId: "act_a", sessionKey: SESSION, text: "draft A", nonce: 1 };
const B: DraftEdit = { actionId: "act_b", sessionKey: SESSION, text: "draft B", nonce: 2 };

function screenFor(prefill: DraftEdit | null, onDraftReplaced: (id: string) => void) {
  return (
    <MissionScreen
      missionId={MISSION}
      sessionKey={SESSION}
      prefill={prefill}
      onDraftReplaced={onDraftReplaced}
    />
  );
}

/** A promise this test resolves by hand, so a response can land after the next Edit. */
function deferred<T>() {
  let settle!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    settle = res;
  });
  return { promise, settle };
}

const DELIVERED = {
  action_id: "relay_1",
  state: "delivered",
  detail: "",
  session_key: SESSION,
};

beforeEach(() => {
  vi.mocked(api.relayToSession).mockReset();
  // jsdom implements neither, and the composer uses both when a draft is adopted.
  Element.prototype.scrollIntoView = vi.fn();
  window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    cb(0);
    return 0;
  }) as typeof window.requestAnimationFrame;
});

test("a late answer for one draft never clears a newer draft's replacement", async () => {
  const a = deferred<Awaited<ReturnType<typeof api.relayToSession>>>();
  vi.mocked(api.relayToSession).mockReturnValueOnce(a.promise);
  const replaced = vi.fn();
  const { rerender } = render(screenFor(A, replaced));

  await userEvent.click(screen.getByTestId("relay-send"));
  expect(api.relayToSession).toHaveBeenCalledWith(MISSION, SESSION, "draft A", "act_a");

  // Edit hands the composer a DIFFERENT draft while A's send is still out.
  rerender(screenFor(B, replaced));
  expect(screen.getByTestId("relay-input")).toHaveValue("draft B");

  await act(async () => {
    a.settle({ ...DELIVERED, state: "failed", detail: "a viewer is attached", draft_replaced: true });
  });

  // A's own outcome is still announced: the server closed it, so its card must go.
  expect(replaced).toHaveBeenCalledWith("act_a");
  // …and B is untouched, text and replacement alike.
  expect(screen.getByTestId("relay-input")).toHaveValue("draft B");
  expect(screen.getByTestId("relay-draft-edit")).toBeInTheDocument();

  vi.mocked(api.relayToSession).mockResolvedValueOnce({ ...DELIVERED, draft_replaced: true });
  await userEvent.click(screen.getByTestId("relay-send"));
  expect(api.relayToSession).toHaveBeenLastCalledWith(MISSION, SESSION, "draft B", "act_b");
});

test("a delivered ordinary relay does not erase a draft adopted while it was in flight", async () => {
  const first = deferred<Awaited<ReturnType<typeof api.relayToSession>>>();
  vi.mocked(api.relayToSession).mockReturnValueOnce(first.promise);
  const replaced = vi.fn();
  const { rerender } = render(screenFor(null, replaced));

  await userEvent.type(screen.getByTestId("relay-input"), "are you there?");
  await userEvent.click(screen.getByTestId("relay-send"));
  expect(api.relayToSession).toHaveBeenCalledWith(MISSION, SESSION, "are you there?", undefined);

  rerender(screenFor(B, replaced));
  expect(screen.getByTestId("relay-input")).toHaveValue("draft B");

  // `delivered` is the one outcome that empties the box — of the message that WAS sent.
  await act(async () => {
    first.settle(DELIVERED);
  });
  expect(screen.getByTestId("relay-input")).toHaveValue("draft B");
  expect(replaced).not.toHaveBeenCalled();

  vi.mocked(api.relayToSession).mockResolvedValueOnce({ ...DELIVERED, draft_replaced: true });
  await userEvent.click(screen.getByTestId("relay-send"));
  expect(api.relayToSession).toHaveBeenLastCalledWith(MISSION, SESSION, "draft B", "act_b");
});
