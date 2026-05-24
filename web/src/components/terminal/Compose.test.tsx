import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { bracketedPaste, KEYSEQ } from "../../lib/termKeys";
import { Compose } from "./Compose";

vi.mock("../../lib/api", () => ({ api: { upload: vi.fn() } }));

let sendInput: ReturnType<typeof vi.fn>;
let onCopy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  sendInput = vi.fn();
  onCopy = vi.fn();
});

function renderCompose() {
  return render(<Compose sendInput={sendInput} onCopy={onCopy} />);
}

test("nav keys send their control sequence to the PTY", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: "Up" }));
  await user.click(screen.getByRole("button", { name: /ctrl-c/i }));
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.up);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.ctrlc);
});

test("Send clears the line then bracketed-pastes the message + Enter", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  expect(sendInput).toHaveBeenNthCalledWith(1, KEYSEQ.ctrla + KEYSEQ.ctrlk);
  expect(sendInput).toHaveBeenNthCalledWith(2, `${bracketedPaste("hello world")}${KEYSEQ.enter}`);
});

test("Enter sends, Shift+Enter inserts a newline", async () => {
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox");
  await user.type(ta, "line1{Shift>}{Enter}{/Shift}line2");
  expect(sendInput).not.toHaveBeenCalled(); // shift+enter = newline, not send
  await user.type(ta, "{Enter}");
  expect(sendInput).toHaveBeenCalledWith(expect.stringContaining(bracketedPaste("line1\nline2")));
});

test("empty Send is a no-op", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: /^send/i }));
  expect(sendInput).not.toHaveBeenCalled();
});

test("the compose toggle hides/shows the text field", async () => {
  const user = userEvent.setup();
  renderCompose();
  expect(screen.getByRole("textbox")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /collapse compose/i }));
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

test("the copy button invokes onCopy", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: /copy/i }));
  expect(onCopy).toHaveBeenCalledOnce();
});
