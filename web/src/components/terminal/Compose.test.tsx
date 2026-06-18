import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { bracketedPaste, KEYSEQ } from "../../lib/termKeys";
import { Compose, type ComposeHandle } from "./Compose";

vi.mock("../../lib/api", () => ({ api: { upload: vi.fn() } }));

let sendInput: ReturnType<typeof vi.fn>;
let onCopy: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks();
  sendInput = vi.fn(() => true); // default: every frame is delivered (socket OPEN)
  onCopy = vi.fn();
});

function renderCompose(connEpoch: () => number = () => 1) {
  return render(<Compose sendInput={sendInput} connEpoch={connEpoch} onCopy={onCopy} />);
}

test("nav keys send their control sequence to the PTY", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: "Up" }));
  await user.click(screen.getByRole("button", { name: /ctrl-c/i }));
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.up);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.ctrlc);
});

test("interrupt button is icon-only — no visible 'Interrupt' label (#186)", () => {
  renderCompose();
  const btn = screen.getByRole("button", { name: /ctrl-c/i });
  // The icon-only invariant: aria-label + title are the affordance, no visible text.
  expect(btn.textContent ?? "").toBe("");
  expect(btn).toHaveAttribute("aria-label", expect.stringMatching(/interrupt/i));
  expect(btn).toHaveAttribute("title", expect.stringMatching(/interrupt/i));
});

test("Send clears the line, bracketed-pastes the message, then submits a DEFERRED Enter (#180)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // Clear + paste go out synchronously; the Enter is deferred to a later frame so the agent
  // can't read the trailing ``\r`` as still inside the bracketed-paste buffer (the "press Enter
  // twice" bug). Three discrete WS frames, never paste-end + \r in one packet.
  expect(sendInput).toHaveBeenNthCalledWith(1, KEYSEQ.ctrla + KEYSEQ.ctrlk);
  expect(sendInput).toHaveBeenNthCalledWith(2, bracketedPaste("hello world"));
  expect(sendInput).toHaveBeenCalledTimes(2); // Enter not sent yet
  await waitFor(() => expect(sendInput).toHaveBeenNthCalledWith(3, KEYSEQ.enter));
});

test("does NOT submit a bare Enter when the paste wasn't delivered — no empty turn (#287)", async () => {
  // The empty-compose bug: a clear/paste sent mid-reconnect is dropped, but the deferred Enter still
  // lands on the reconnected socket → an empty turn. When the paste isn't delivered we must abort and
  // KEEP the text, never fire the Enter.
  const user = userEvent.setup();
  sendInput = vi.fn(() => false); // socket down → nothing delivers
  renderCompose();
  const ta = screen.getByRole("textbox");
  await user.type(ta, "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // clear + paste were attempted, but NO Enter is ever scheduled.
  await new Promise((r) => setTimeout(r, 200));
  expect(sendInput).not.toHaveBeenCalledWith(KEYSEQ.enter);
  expect((ta as HTMLTextAreaElement).value).toBe("hello world"); // text preserved for a retry
});

test("re-sends clear+paste on the new socket before Enter if a reconnect split the frames (#287)", async () => {
  // The paste went to socket A, then a reconnect → the deferred Enter would hit socket B which never
  // got the paste. Detect the socket-id change and re-send clear+paste on B before submitting.
  const user = userEvent.setup();
  let epoch = 1;
  renderCompose(() => epoch);
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  expect(sendInput).toHaveBeenCalledTimes(2); // clear + paste on socket A
  epoch = 2; // a reconnect happens before the deferred Enter fires
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter));
  // The deferred batch re-sent clear+paste (on B) AND then the Enter — 5 calls total, Enter last.
  const calls = sendInput.mock.calls.map((c) => c[0]);
  expect(calls).toEqual([
    KEYSEQ.ctrla + KEYSEQ.ctrlk,
    bracketedPaste("hello world"),
    KEYSEQ.ctrla + KEYSEQ.ctrlk,
    bracketedPaste("hello world"),
    KEYSEQ.enter,
  ]);
});

test("a SECOND reconnect during the deferred retry still never submits an empty Enter (#287)", async () => {
  // The re-paste on the new socket can ALSO fail if a second reconnect lands. Mirror the first-send
  // guard: no Enter, and restore the text for a retry.
  const user = userEvent.setup();
  let pastes = 0;
  sendInput = vi.fn((d) => {
    if (d === bracketedPaste("hello world")) {
      pastes += 1;
      return pastes === 1; // first paste delivers; the re-paste (2nd) does NOT
    }
    return true;
  });
  let epoch = 1;
  renderCompose(() => epoch);
  const ta = screen.getByRole("textbox");
  await user.type(ta, "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  epoch = 2; // reconnect before the deferred Enter → re-paste path, which then fails to deliver
  await waitFor(() => expect((ta as HTMLTextAreaElement).value).toBe("hello world")); // text restored
  expect(sendInput).not.toHaveBeenCalledWith(KEYSEQ.enter);
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

test("Send with image attachment writes Enter as its own DEFERRED frame after the paste (#180/#197)", async () => {
  // Regression for the original bug (#180): with an attachment path appended to
  // the text the bracketed-paste packet got long enough that some agents read the
  // trailing ``\r`` as still inside the paste buffer, leaving the prompt typed but
  // unsubmitted. #197: splitting the frame was necessary but not sufficient — the
  // agent ingests the pasted image path asynchronously, so the Enter must also be
  // DEFERRED past the paste, or it still races ingestion and is dropped.
  const user = userEvent.setup();
  vi.mocked(api.upload).mockResolvedValue({ name: "shot.png", path: "/uploads/shot.png" });
  const handle = createRef<ComposeHandle>();
  render(<Compose ref={handle} sendInput={sendInput} onCopy={onCopy} />);
  await user.type(screen.getByRole("textbox"), "look at this");
  // Forward an image paste the way Terminal does — the upload resolves and the
  // attachment pill renders.
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  handle.current!.attachImages([file]);
  await screen.findByText("shot.png");

  sendInput.mockClear();
  await user.click(screen.getByRole("button", { name: /^send/i }));

  // The clear + paste frames go out synchronously; the Enter does NOT — it's
  // deferred so the agent finishes ingesting the image path first (#197).
  expect(sendInput).toHaveBeenNthCalledWith(1, KEYSEQ.ctrla + KEYSEQ.ctrlk);
  expect(sendInput).toHaveBeenNthCalledWith(
    2,
    bracketedPaste("look at this /uploads/shot.png"),
  );
  expect(sendInput).toHaveBeenCalledTimes(2); // Enter not sent yet
  // …it arrives shortly after as its own discrete frame.
  await waitFor(() => expect(sendInput).toHaveBeenNthCalledWith(3, KEYSEQ.enter));
  // No frame ever contains the paste-end marker + Enter back-to-back.
  const pasteEndPlusEnter = "\x1b[201~" + KEYSEQ.enter;
  for (const [arg] of sendInput.mock.calls) {
    expect(String(arg).includes(pasteEndPlusEnter)).toBe(false);
  }
});

test("empty Send acts as a bare Return — submits console-typed input (#474)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // With nothing in the compose box, Send is a single \r so it submits whatever the user typed
  // directly into the console — NOT a clear/paste/deferred-Enter (that would erase the line).
  expect(sendInput).toHaveBeenCalledTimes(1);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter);
  expect(sendInput).not.toHaveBeenCalledWith(KEYSEQ.ctrla + KEYSEQ.ctrlk);
  // Give any (incorrect) deferred Enter a chance to fire; there must be no second frame.
  await new Promise((r) => setTimeout(r, 200));
  expect(sendInput).toHaveBeenCalledTimes(1);
});

test("empty Send mid-reconnect surfaces the note and sends nothing else (#474)", async () => {
  const user = userEvent.setup();
  sendInput = vi.fn(() => false); // socket down → the bare Enter isn't delivered
  renderCompose();
  await user.click(screen.getByRole("button", { name: /^send/i }));
  expect(sendInput).toHaveBeenCalledTimes(1);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter);
  expect(await screen.findByText(/reconnecting — not sent/i)).toBeInTheDocument();
});

test("the compose toggle hides/shows the text field", async () => {
  const user = userEvent.setup();
  renderCompose();
  expect(screen.getByRole("textbox")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /collapse compose/i }));
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

test("pasting an image uploads it and adds an attachment, not text (#135)", async () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  vi.mocked(api.upload).mockResolvedValue({ name: "shot.png", path: "/uploads/shot.png" });
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  fireEvent.paste(ta, {
    clipboardData: {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      files: [file],
    },
  });
  expect(api.upload).toHaveBeenCalledWith(file);
  // The upload surfaces as an attachment pill (compose box open) — and no text was inserted.
  expect(await screen.findByText("shot.png")).toBeInTheDocument();
  expect(ta.value).toBe("");
});

test("pasting plain text is left to the textarea (no upload)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("textbox"));
  await user.paste("just text");
  expect(api.upload).not.toHaveBeenCalled();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toContain("just text");
});

test("the copy button invokes onCopy", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: /copy/i }));
  expect(onCopy).toHaveBeenCalledOnce();
});

test("attachImages opens the compose (if collapsed) and adds the upload as a pill (#157)", async () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  vi.mocked(api.upload).mockResolvedValue({ name: "shot.png", path: "/uploads/shot.png" });
  const ref = createRef<ComposeHandle>();
  render(<Compose ref={ref} sendInput={sendInput} onCopy={onCopy} defaultOpen={false} />);
  // Desktop-style: collapsed → no textarea visible.
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  ref.current!.attachImages([file]);
  // Compose expands → textarea is now visible, and the upload surfaces as a pill.
  await screen.findByRole("textbox");
  await screen.findByText("shot.png");
  expect(api.upload).toHaveBeenCalledWith(file);
  // No bracketed-paste of the path into the PTY — pill mode only.
  expect(sendInput).not.toHaveBeenCalledWith(`${bracketedPaste("/uploads/shot.png")} `);
});

test("attachImages with no files is a no-op", () => {
  const ref = createRef<ComposeHandle>();
  render(<Compose ref={ref} sendInput={sendInput} onCopy={onCopy} defaultOpen={false} />);
  ref.current!.attachImages([]);
  expect(api.upload).not.toHaveBeenCalled();
});
