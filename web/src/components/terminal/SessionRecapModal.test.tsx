import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { SessionRecapModal } from "./SessionRecapModal";

function renderModal(overrides: Partial<Parameters<typeof SessionRecapModal>[0]> = {}) {
  const onClose = vi.fn();
  const trigger = document.createElement("button");
  trigger.textContent = "Open";
  document.body.appendChild(trigger);
  trigger.focus();
  const result = render(
    <SessionRecapModal
      sessionId="claude:abc"
      engine="claude"
      title="Fix the auth token refresh race"
      project="agent-sessions"
      summary="Refactoring the token-refresh path."
      recap={"Cloned repo.\nFixed bug."}
      reviewedAt={1_700_000_000}
      onClose={onClose}
      returnFocusTo={trigger}
      {...overrides}
    />,
  );
  return { ...result, onClose, trigger };
}

afterEach(() => vi.restoreAllMocks());

test("shows the full title, summary, and the chronological recap (#481)", () => {
  renderModal();
  expect(
    screen.getByRole("dialog", { name: /fix the auth token refresh race/i }),
  ).toBeInTheDocument();
  expect(screen.getByText("Refactoring the token-refresh path.")).toBeInTheDocument();
  // The recap keeps its newline-separated timeline (rendered as one pre-wrap block).
  expect(screen.getByText(/Cloned repo\./)).toBeInTheDocument();
  expect(screen.getByText(/Fixed bug\./)).toBeInTheDocument();
});

test("an empty recap shows the 'no recap yet' state (#481)", () => {
  renderModal({ recap: "" });
  expect(screen.getByText(/no recap yet/i)).toBeInTheDocument();
});

test("the intervention chip renders only when required (#481)", () => {
  renderModal({ interventionRequired: true, interventionReason: "waiting on permission" });
  expect(screen.getByText(/needs you/i)).toBeInTheDocument();
  expect(screen.getByText(/waiting on permission/i)).toBeInTheDocument();
});

test("Esc, the backdrop, and the close button all close it (#481 a11y)", async () => {
  const { onClose, baseElement } = renderModal();
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
  const backdrop = baseElement.querySelector('[class*="backdrop"]') as HTMLElement;
  await userEvent.click(backdrop);
  expect(onClose).toHaveBeenCalledTimes(2);
  await userEvent.click(screen.getByRole("button", { name: /close session brief/i }));
  expect(onClose).toHaveBeenCalledTimes(3);
});

test("clicking inside the dialog does NOT close it (#481)", async () => {
  const { onClose } = renderModal();
  await userEvent.click(screen.getByRole("dialog"));
  expect(onClose).not.toHaveBeenCalled();
});

test("focus returns to the trigger on unmount (#481 a11y)", () => {
  const { unmount, trigger } = renderModal();
  (document.activeElement as HTMLElement | null)?.blur?.();
  unmount();
  expect(document.activeElement).toBe(trigger);
});

test("'Review now' refreshes the recap in place (#481)", async () => {
  const spy = vi.spyOn(api, "reviewNow").mockResolvedValue({
    id: "claude:abc",
    title: "Fix the auth token refresh race",
    ai_summary: "Updated summary.",
    ai_title: "",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: 1_700_000_500,
    review_excluded: false,
    ai_recap: "Re-reviewed everything.\nAll green now.",
    recap_fingerprint: "fp2",
  });
  renderModal({ recap: "" });
  expect(screen.getByText(/no recap yet/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /review now/i }));
  expect(spy).toHaveBeenCalledWith("claude:abc");
  await waitFor(() => expect(screen.getByText(/all green now/i)).toBeInTheDocument());
  expect(screen.getByText(/updated summary/i)).toBeInTheDocument();
});

test("a review-excluded session shows the excluded state and disables Review now (#481)", () => {
  renderModal({ recap: "", reviewExcluded: true });
  expect(screen.getByText(/excluded from ai review/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /review now/i })).toBeDisabled();
});
