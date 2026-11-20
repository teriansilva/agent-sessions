/** The tabbed question block and its pinned dock (#2234): one question shows at a time, answers
 *  survive tab switches, a single-choice pick moves on, Send still waits for every answer, and a
 *  docked question leaves only a marker in the transcript. Layout (the dock staying in view while
 *  the log scrolls) is proved in a real browser (`e2e/structured-question-dock.spec.ts`). */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, test, vi } from "vitest";

import { DockedQuestion, QuestionDock } from "./QuestionDock";
import { ChoiceQuestions, type ChoiceQuestion } from "./Questions";
import { QuestionDockTarget } from "./questionDockContext";

const QS: ChoiceQuestion[] = [
  { header: "Access", title: "How does he reach the Mac?", options: [{ label: "SSH" }, { label: "LAN" }] },
  { header: "Network", title: "Which networks are blocked?", options: [{ label: "All private" }, { label: "Only two" }] },
  { header: "sudo", title: "Where does the password come from?", options: [{ label: "Vault" }, { label: "You" }] },
];

function block(onSubmit = vi.fn(), questions = QS) {
  render(
    <ChoiceQuestions heading="Claude Code is waiting for your answer" questions={questions} allowOther
      disabled={false} submitLabel="Send answers" onSubmit={onSubmit} />,
  );
  return onSubmit;
}

const visibleOptions = () =>
  screen.getAllByTestId("structured-question-option").filter((o) => !o.closest("[hidden]"));

test("several questions get one tab each, and only the active question's options show", () => {
  block();
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((t) => t.textContent)).toEqual([
    "Access, not answered", "Network, not answered", "sudo, not answered",
  ]);
  expect(tabs[0]).toHaveAttribute("aria-selected", "true");
  expect(visibleOptions().map((o) => o.textContent)).toEqual(["SSH", "LAN"]);
  expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Access, not answered");
});

test("a single-choice pick moves to the next unanswered question; answers survive switching back", async () => {
  const user = userEvent.setup();
  const onSubmit = block();
  await user.click(screen.getByLabelText("SSH"));
  expect(screen.getAllByRole("tab")[1]).toHaveAttribute("aria-selected", "true");
  expect(screen.getAllByRole("tab")[0]).toHaveAttribute("data-answered", "true");
  expect(screen.getByTestId("structured-question")).toHaveTextContent("1 of 3 answered");
  // Send waits for every question, hidden tabs included, and names what is missing.
  expect(screen.getByTestId("structured-question-send")).toBeDisabled();
  expect(screen.getByTestId("structured-question")).toHaveTextContent("Answer “Network”, “sudo” to send.");
  await user.click(screen.getAllByRole("tab")[0]);
  expect(screen.getByLabelText("SSH")).toBeChecked();
  // The last open question after the active one wraps around to it.
  await user.click(screen.getAllByRole("tab")[2]);
  await user.click(screen.getByLabelText("You"));
  expect(screen.getAllByRole("tab")[1]).toHaveAttribute("aria-selected", "true");
  await user.click(screen.getByLabelText("Only two"));
  await user.click(screen.getByTestId("structured-question-send"));
  expect(onSubmit).toHaveBeenCalledWith([
    { selected: ["SSH"] }, { selected: ["Only two"] }, { selected: ["You"] },
  ]);
});

test("typing an own answer does not move away from the question", async () => {
  const user = userEvent.setup();
  block();
  const own = screen.getAllByTestId("structured-question-other").find((o) => !o.closest("[hidden]"))!;
  await user.type(own, "Tailscale");
  expect(screen.getAllByRole("tab")[0]).toHaveAttribute("aria-selected", "true");
  expect(screen.getAllByRole("tab")[0]).toHaveAttribute("data-answered", "true");
});

test("arrow keys, Home and End move between tabs (roving focus)", async () => {
  const user = userEvent.setup();
  block();
  const tabs = () => screen.getAllByRole("tab");
  tabs()[0].focus();
  await user.keyboard("{ArrowRight}");
  expect(tabs()[1]).toHaveFocus();
  expect(tabs()[1]).toHaveAttribute("aria-selected", "true");
  expect(tabs()[0]).toHaveAttribute("tabindex", "-1");
  await user.keyboard("{ArrowLeft}{ArrowLeft}");
  expect(tabs()[2]).toHaveFocus();
  await user.keyboard("{Home}");
  expect(tabs()[0]).toHaveFocus();
  await user.keyboard("{End}");
  expect(tabs()[2]).toHaveFocus();
});

test("one question has no tab strip", () => {
  block(vi.fn(), [QS[0]]);
  expect(screen.queryByRole("tablist")).toBeNull();
  expect(visibleOptions()).toHaveLength(2);
  // No dock around it: no minimise toggle.
  expect(screen.queryByTestId("structured-question-minimise")).toBeNull();
});

function Pane({ withDock = true }: { withDock?: boolean }) {
  const [dock, setDock] = useState<HTMLDivElement | null>(null);
  return (
    <>
      <QuestionDockTarget.Provider value={withDock ? dock : null}>
        <div data-testid="log">
          <DockedQuestion label="Claude Code is waiting for your answer">
            <ChoiceQuestions heading="Claude Code is waiting for your answer" questions={QS}
              disabled={false} submitLabel="Send answers" onSubmit={() => {}} />
          </DockedQuestion>
        </div>
      </QuestionDockTarget.Provider>
      <QuestionDock dockRef={setDock} />
    </>
  );
}

test("a docked question renders in the dock; the log keeps only its marker", () => {
  render(<Pane />);
  const log = screen.getByTestId("log");
  const dock = screen.getByTestId("structured-question-dock");
  expect(within(dock).getByTestId("structured-question")).toBeInTheDocument();
  expect(within(log).queryByTestId("structured-question")).toBeNull();
  expect(within(log).getByTestId("structured-question-marker")).toHaveTextContent(
    "Claude Code is waiting for your answer· answer below ↓",
  );
});

test("without a dock the question stays inline, as before", () => {
  render(<Pane withDock={false} />);
  expect(within(screen.getByTestId("log")).getByTestId("structured-question")).toBeInTheDocument();
  expect(screen.queryByTestId("structured-question-marker")).toBeNull();
});

test("minimise keeps the head and answers; the marker expands it again and focuses the active tab", async () => {
  const user = userEvent.setup();
  render(<Pane />);
  await user.click(screen.getByLabelText("SSH"));
  const toggle = screen.getByTestId("structured-question-minimise");
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await user.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("tablist")).toBeNull();
  expect(screen.queryByTestId("structured-question-send")).toBeNull();
  expect(screen.getByTestId("structured-question")).toHaveTextContent("1 of 3 answered");
  await user.click(screen.getByTestId("structured-question-marker"));
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await vi.waitFor(() => expect(screen.getAllByRole("tab")[1]).toHaveFocus());
  // The answer given before minimising is still there.
  await user.click(screen.getAllByRole("tab")[0]);
  expect(screen.getByLabelText("SSH")).toBeChecked();
});
