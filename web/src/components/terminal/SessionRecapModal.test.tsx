import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { SessionRecapModal } from "./SessionRecapModal";

function renderModal(
  overrides: Partial<Parameters<typeof SessionRecapModal>[0]> = {},
) {
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
      project={{ kind: "project", id: "p-1", name: "agent-sessions" }}
      lastMtime={Math.floor(Date.now() / 1000) - 7200}
      statusRow={{ review_excluded: false, working: true, has_draft: false }}
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
  expect(
    screen.getByText("Refactoring the token-refresh path."),
  ).toBeInTheDocument();
  expect(screen.getByText(/Cloned repo\./)).toBeInTheDocument();
  expect(screen.getByText(/Fixed bug\./)).toBeInTheDocument();
});

test("an empty recap shows the 'no recap yet' state (#481)", () => {
  renderModal({ recap: "" });
  expect(screen.getByText(/no recap yet/i)).toBeInTheDocument();
});

// #744: the brief now carries the sidebar row's whole identity, so re-entering a session tells
// you what it is without going back to the list.
test("the meta line carries engine, project, updated and reviewed (#744)", () => {
  renderModal();
  const dialog = screen.getByRole("dialog");
  // Engine as the sidebar's short badge, not the spelled-out word it used to print.
  expect(screen.getByTitle("claude")).toHaveTextContent("cc");
  expect(dialog).not.toHaveTextContent("CLAUDE");
  expect(screen.getByText("agent-sessions")).toBeInTheDocument();
  expect(screen.getByText(/updated 2 hours ago/i)).toBeInTheDocument();
  expect(screen.getByText(/reviewed /i)).toBeInTheDocument();
  // State is never colour-only: the LED keeps an accessible name.
  expect(
    screen.getByRole("status", { name: /agent working/i }),
  ).toBeInTheDocument();
});

// The status shown here is the SESSION's (sidebar parity), never this browser's socket state.
// Opened from the sidebar there is no socket at all, so a socket-derived dot would be a lie.
test("with no resolved session status the brief shows no dot at all (#744)", () => {
  renderModal({ statusRow: undefined });
  expect(screen.getByRole("dialog").querySelector(".hud-led")).toBeNull();
  // …and the rest of the meta line still renders — an unavailable dot isn't a broken brief.
  expect(screen.getByText("agent-sessions")).toBeInTheDocument();
});

test("an intervention session shows the attention dot with its reason as the name (#744)", () => {
  renderModal({
    statusRow: { review_excluded: false, working: false, has_draft: false },
    interventionRequired: true,
    interventionReason: "waiting on permission",
  });
  const led = screen.getByRole("img", {
    name: /intervention required: waiting on permission/i,
  });
  expect(led).toHaveClass("attention");
});

test("an idle dot is hidden from assistive tech rather than announced as nothing (#744)", () => {
  renderModal({
    statusRow: { review_excluded: false, working: false, has_draft: false },
  });
  const led = screen.getByRole("dialog").querySelector(".hud-led")!;
  expect(led).toHaveAttribute("aria-hidden", "true");
});

test("the AI summary is the subtitle — no separate SUMMARY section (#744)", () => {
  renderModal();
  expect(
    screen.getByText("Refactoring the token-refresh path."),
  ).toBeInTheDocument();
  expect(screen.queryByText("Summary")).not.toBeInTheDocument();
});

test("an unreviewed session still shows a subtitle placeholder (#744)", () => {
  renderModal({ summary: "", reviewedAt: null });
  expect(screen.getByText(/no summary yet/i)).toBeInTheDocument();
  expect(screen.getByText(/not reviewed yet/i)).toBeInTheDocument();
});

test("a folder-only project shortens the cwd and skips the colour dot (#744)", () => {
  renderModal({
    project: {
      kind: "folder",
      id: "/home/u/proj/api",
      name: "/home/u/proj/api",
    },
  });
  expect(screen.getByText("~/proj/api")).toBeInTheDocument();
});

// The recap is a TIMELINE now: one <li> per line, in order, last step marked.
test("the recap renders as an ordered timeline, one step per line (#744)", () => {
  renderModal({ recap: "First this.\nThen that.\nNow waiting on review." });
  const steps = screen.getAllByRole("listitem");
  expect(steps.map((li) => li.textContent)).toEqual([
    "First this.",
    "Then that.",
    "Now waiting on review.",
  ]);
  // Blank / whitespace-only lines never become empty steps.
  expect(screen.getByRole("list")).toBeInTheDocument();
});

test("blank lines in a recap do not become empty timeline steps (#744)", () => {
  renderModal({ recap: "One.\n\n   \nTwo.\n" });
  expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    "One.",
    "Two.",
  ]);
});

test("a recap step renders the safe inline subset and nothing else (#744)", () => {
  renderModal({
    recap: "**Fixed** the race in `auth.ts` <script>alert(1)</script>",
  });
  const step = screen.getAllByRole("listitem")[0];
  expect(step.querySelector("strong")).toHaveTextContent("Fixed");
  expect(step.querySelector("code")).toHaveTextContent("auth.ts");
  // The html arrived as DATA and stayed data — no element, just characters.
  expect(step.querySelector("script")).toBeNull();
  expect(step).toHaveTextContent("<script>alert(1)</script>");
});

test("the intervention chip renders only when required (#481)", () => {
  renderModal({
    interventionRequired: true,
    interventionReason: "waiting on permission",
  });
  expect(screen.getByText(/needs you/i)).toBeInTheDocument();
  expect(screen.getByText(/waiting on permission/i)).toBeInTheDocument();
});

test("Esc, the backdrop, and the close button all close it (#481 a11y)", async () => {
  const { onClose, baseElement } = renderModal();
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
  const backdrop = baseElement.querySelector(
    '[class*="backdrop"]',
  ) as HTMLElement;
  await userEvent.click(backdrop);
  expect(onClose).toHaveBeenCalledTimes(2);
  await userEvent.click(
    screen.getByRole("button", { name: /close session brief/i }),
  );
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
  await waitFor(() =>
    expect(screen.getByText(/all green now/i)).toBeInTheDocument(),
  );
  expect(screen.getByText(/updated summary/i)).toBeInTheDocument();
});

// The header LED and the intervention panel are two views of ONE fact, so a review that changes
// that fact has to move both together. The row's status is a snapshot from the last store poll;
// only the modal knows the result of the review it just ran.
test("'Review now' clearing intervention drops the attention dot to the row's own state (#745)", async () => {
  vi.spyOn(api, "reviewNow").mockResolvedValue({
    id: "claude:abc",
    title: "Fix the auth token refresh race",
    ai_summary: "Unblocked.",
    ai_title: "",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: 1_700_000_500,
    review_excluded: false,
    ai_recap: "Answered the prompt.",
    recap_fingerprint: "fp2",
  });
  // Row says attention; underneath it the agent is still working — that's the fallback the dot
  // must land on once the intervention clears, NOT idle and NOT a stale attention.
  renderModal({
    statusRow: { review_excluded: false, working: true, has_draft: false },
    interventionRequired: true,
    interventionReason: "waiting on permission",
  });
  expect(
    screen.getByRole("img", { name: /intervention required/i }),
  ).toHaveClass("attention");

  await userEvent.click(screen.getByRole("button", { name: /review now/i }));

  // The panel's own claim clears…
  await waitFor(() =>
    expect(screen.queryByText(/waiting on permission/i)).toBeNull(),
  );
  // …and the dot agrees instead of contradicting it, without waiting for the next store poll.
  const led = screen.getByRole("dialog").querySelector(".hud-led")!;
  expect(led).not.toHaveClass("attention");
  expect(led).toHaveClass("up");
});

test("'Review now' raising intervention shows it on the dot immediately (#745)", async () => {
  vi.spyOn(api, "reviewNow").mockResolvedValue({
    id: "claude:abc",
    title: "Fix the auth token refresh race",
    ai_summary: "Blocked.",
    ai_title: "",
    intervention_required: true,
    intervention_reason: "needs a decision on the schema",
    reviewed_at: 1_700_000_500,
    review_excluded: false,
    ai_recap: "Hit a fork.",
    recap_fingerprint: "fp3",
  });
  renderModal(); // row: working, no intervention
  expect(
    screen.getByRole("status", { name: /agent working/i }),
  ).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /review now/i }));

  await waitFor(() =>
    expect(
      screen.getByRole("img", {
        name: /intervention required: needs a decision on the schema/i,
      }),
    ).toHaveClass("attention"),
  );
});

test("a review-excluded session shows the excluded state and disables Review now (#481)", () => {
  renderModal({ recap: "", reviewExcluded: true });
  // #744: it now reads in two places, answering two questions — the subtitle says what this
  // session IS (sidebar parity), the recap slot says why it is empty.
  expect(screen.getByText("Excluded from AI review")).toBeInTheDocument();
  expect(
    screen.getByText("This session is excluded from AI review."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /review now/i })).toBeDisabled();
});
