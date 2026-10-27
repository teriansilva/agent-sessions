/** The HQ composer with shared files (#1707) — the staging state machine and the #1605 guard.
 *
 *  Pinned here: a file stages as a pill (uploading → ready) and its id rides the message; a
 *  failure keeps a NAMED pill that blocks Send AND Enter until it is removed; removing the failed
 *  pill unblocks; a file-only message is a message; a blocked attempt never fires later on its own;
 *  the camera picker is `accept="image/*" capture="environment"` (the OS picker — no getUserMedia);
 *  removal controls are labeled. Everything else about the box is pinned in AskConsole.test.tsx.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { AskComposer } from "./AskComposer";
import type { AskAttachment } from "../../types/api";

const ATT: AskAttachment = {
  id: "a".repeat(32),
  name: "deploy.log",
  kind: "text",
  size: 12,
};

function mount(over: Partial<Parameters<typeof AskComposer>[0]> = {}) {
  const onAsk = vi.fn();
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  render(
    <AskComposer
      configured
      busy={false}
      onAsk={onAsk}
      uploadFile={uploadFile}
      {...over}
    />,
  );
  return { onAsk, uploadFile };
}

function pick(file: File) {
  const input = screen.getByTestId("ask-attach-input") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("a file stages as a pill and its id rides the message", async () => {
  const { onAsk, uploadFile } = mount();
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() =>
    expect(screen.getByTestId("ask-staged-pill")).toBeTruthy(),
  );
  await waitFor(() =>
    expect(screen.getByText("Ready")).toBeTruthy(),
  );
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "what failed?" },
  });
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  expect(onAsk.mock.calls[0][0]).toBe("what failed?");
  expect(onAsk.mock.calls[0][1]).toEqual([ATT]);
  expect(uploadFile).toHaveBeenCalledTimes(1);
});

test("a file-only message is a message", async () => {
  const { onAsk } = mount();
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  const send = screen.getByTestId("composer-send") as HTMLButtonElement;
  expect(send.disabled).toBe(false); // no text, one ready file: Send is live
  fireEvent.click(send);
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  expect(onAsk.mock.calls[0][0]).toBe("");
  expect(onAsk.mock.calls[0][1]).toEqual([ATT]);
});

test("a failed upload keeps a named pill and blocks Send and Enter until it is removed", async () => {
  const { onAsk } = mount({
    uploadFile: vi.fn(async () => {
      throw new Error("nope");
    }),
  });
  pick(new File(["hello"], "notes.docx", { type: "application/msword" }));
  await waitFor(() =>
    expect(screen.getByText("Couldn't read that file")).toBeTruthy(),
  );
  // named: the pill says WHICH file failed
  expect(screen.getByText("notes.docx")).toBeTruthy();
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "send anyway" },
  });
  const send = screen.getByTestId("composer-send") as HTMLButtonElement;
  expect(send.disabled).toBe(true);
  // the keyboard path takes the same guard
  fireEvent.submit(screen.getByTestId("ask-form"));
  expect(onAsk).not.toHaveBeenCalled();

  // the labeled removal is the only unblock
  fireEvent.click(screen.getByLabelText("Remove notes.docx"));
  await waitFor(() =>
    expect(screen.getByTestId("composer-send").hasAttribute("disabled")).toBe(
      false,
    ),
  );
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  expect(onAsk.mock.calls[0][1]).toEqual([]); // the failed file is gone, the rest sends
});

test("while a file uploads, Send is blocked and a blocked attempt never fires later", async () => {
  let release: (a: { attachment: AskAttachment; cid: string }) => void = () => {};
  const uploadFile = vi.fn(
    () =>
      new Promise<{ attachment: AskAttachment; cid: string }>((resolve) => (release = resolve)),
  );
  const { onAsk } = mount({ uploadFile });
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Uploading…")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "early" },
  });
  fireEvent.click(screen.getByTestId("composer-send"));
  fireEvent.submit(screen.getByTestId("ask-form"));
  expect(onAsk).not.toHaveBeenCalled();
  release({ attachment: ATT, cid: "ask_aaa" });
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  // unblocked NOW, but nothing sent on its own — one explicit Send does
  expect(onAsk).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
});

test("the camera picker is the OS capture picker and takes only images", () => {
  mount();
  const cam = screen.getByTestId("ask-camera-input") as HTMLInputElement;
  expect(cam.getAttribute("accept")).toBe("image/*");
  expect(cam.getAttribute("capture")).toBe("environment");
  // Attach, by contrast, takes anything
  const all = screen.getByTestId("ask-attach-input") as HTMLInputElement;
  expect(all.getAttribute("accept")).toBeNull();
  expect(all.hasAttribute("multiple")).toBe(true);
});

test("every removal control is labeled and removes only its own pill", async () => {
  let n = 0;
  const uploadFile = vi.fn(async () => ({
    attachment: { ...ATT, id: String(++n).padStart(32, "a") },
    cid: "ask_aaa",
  }));
  mount({ uploadFile });
  pick(new File(["a"], "one.txt", { type: "text/plain" }));
  pick(new File(["b"], "two.txt", { type: "text/plain" }));
  await waitFor(() =>
    expect(screen.getAllByTestId("ask-staged-pill")).toHaveLength(2),
  );
  const x = screen.getAllByTestId("ask-staged-remove");
  for (const b of x) expect(b.getAttribute("aria-label")).toMatch(/^Remove /);
  fireEvent.click(screen.getByLabelText("Remove one.txt"));
  await waitFor(() =>
    expect(screen.getAllByTestId("ask-staged-pill")).toHaveLength(1),
  );
  expect(screen.getByText("two.txt")).toBeTruthy();
});


test("staged files belong to their conversation — a switch clears them", async () => {
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  const onAsk = vi.fn();
  const { rerender } = render(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_aaa" />,
  );
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  // switching to another conversation drops the staged files: they are never sent elsewhere
  rerender(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_bbb" />,
  );
  await waitFor(() => expect(screen.queryByTestId("ask-staged-pill")).toBeNull());
  expect(onAsk).not.toHaveBeenCalled();
});


test("Remove discards the server blob so the quota frees at once", async () => {
  const discardFile = vi.fn();
  mount({ discardFile } as never);
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.click(screen.getByLabelText("Remove deploy.log"));
  await waitFor(() =>
    expect(screen.queryByTestId("ask-staged-pill")).toBeNull(),
  );
  expect(discardFile).toHaveBeenCalledWith("ask_aaa", ATT);
});

test("a pill removed mid-upload discards the blob the moment it lands", async () => {
  let release: (a: { attachment: AskAttachment; cid: string }) => void = () => {};
  const uploadFile = vi.fn(() =>
    new Promise<{ attachment: AskAttachment; cid: string }>((r) => (release = r)),
  );
  const discardFile = vi.fn();
  render(<AskComposer configured onAsk={vi.fn()} uploadFile={uploadFile} discardFile={discardFile} />);
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Uploading…")).toBeTruthy());
  fireEvent.click(screen.getByLabelText("Remove deploy.log"));
  await waitFor(() =>
    expect(screen.queryByTestId("ask-staged-pill")).toBeNull(),
  );
  release({ attachment: ATT, cid: "ask_aaa" }); // the upload completes AFTER the pill went
  await waitFor(() => expect(discardFile).toHaveBeenCalledWith("ask_aaa", ATT));
});

test("a conversation switch discards everything staged elsewhere", async () => {
  const discardFile = vi.fn();
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  const { rerender } = render(
    <AskComposer configured onAsk={vi.fn()} uploadFile={uploadFile} discardFile={discardFile} convKey="ask_aaa" />,
  );
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  rerender(
    <AskComposer configured onAsk={vi.fn()} uploadFile={uploadFile} discardFile={discardFile} convKey="ask_bbb" />,
  );
  await waitFor(() => expect(discardFile).toHaveBeenCalledWith("ask_aaa", ATT));
  expect(screen.queryByTestId("ask-staged-pill")).toBeNull();
});


test("a discard names the conversation the file was UPLOADED to, not the one on screen", async () => {
  const discardFile = vi.fn();
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_A" }));
  const { rerender } = render(
    <AskComposer configured onAsk={vi.fn()} uploadFile={uploadFile} discardFile={discardFile} convKey="ask_A" />,
  );
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  // the operator moves on to conversation B and then removes the pill-less leftovers by leaving
  rerender(
    <AskComposer configured onAsk={vi.fn()} uploadFile={uploadFile} discardFile={discardFile} convKey="ask_B" />,
  );
  // the cleanup goes to A (where the blob lives), never to B
  await waitFor(() => expect(discardFile).toHaveBeenCalledWith("ask_A", ATT));
  expect(discardFile).not.toHaveBeenCalledWith("ask_B", expect.anything());
});


test("a confirmed refusal brings the draft back; a sent turn does not", async () => {
  let outcome: "sent" | "refused" = "refused";
  const onAsk = vi.fn(async () => ({ outcome, conv: "ask_aaa" }));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  render(<AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_aaa" />);
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "what failed?" },
  });
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  // the confirmed refusal restores the text AND the same staged file
  await waitFor(() =>
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe(
      "what failed?",
    ),
  );
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());

  // and an accepted send keeps the draft cleared
  outcome = "sent";
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(2));
  await new Promise((r) => setTimeout(r, 10));
  expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe("");
  expect(screen.queryByTestId("ask-staged-pill")).toBeNull();
});

test("files still being freed are said out loud, not silently promised as free", () => {
  render(
    <AskComposer
      configured
      onAsk={vi.fn()}
      uploadFile={vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }))}
      cleanupPending={3}
    />,
  );
  const row = screen.getByTestId("ask-cleanup-pending");
  expect(row.textContent).toContain("3 removed files");
});


test("a refused send never restores over a NEWER draft", async () => {
  let release: (r: { outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }) => void = () => {};
  const onAsk = vi.fn(() => new Promise<{ outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }>((r) => (release = r)));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  render(<AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_aaa" />);
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "old request" },
  });
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  // the operator moves on WHILE the request is in flight
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "newer draft" },
  });
  pick(new File(["new"], "new.txt", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("new.txt")).toBeTruthy());
  release({ outcome: "refused", conv: "ask_aaa" }); // the old request comes back refused
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  // the newer draft and the new file stay untouched — and the refused one is KEPT, not dropped
  expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe(
    "newer draft",
  );
  expect(screen.getByText("new.txt")).toBeTruthy();
  const row = screen.getAllByTestId("ask-recovered")[0];
  expect(row.textContent).toContain("old request");
  expect(row.textContent).toContain("1 file");
  // Restore merges both; nothing is lost and nothing is overwritten
  fireEvent.click(screen.getAllByTestId("ask-recovered-restore")[0]);
  await waitFor(() =>
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toContain(
      "old request",
    ),
  );
  expect(
    (screen.getByTestId("composer-input") as HTMLTextAreaElement).value,
  ).toContain("newer draft");
  expect(screen.getByText("deploy.log")).toBeTruthy();
  expect(screen.getByText("new.txt")).toBeTruthy();
});

test("Discard on the kept refused draft releases its files' quota", async () => {
  let release: (r: { outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }) => void = () => {};
  const onAsk = vi.fn(() => new Promise<{ outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }>((r) => (release = r)));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  const discardFile = vi.fn();
  render(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} discardFile={discardFile} />,
  );
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "old request" },
  });
  fireEvent.click(screen.getByTestId("composer-send"));
  fireEvent.change(screen.getByTestId("composer-input"), {
    target: { value: "newer draft" },
  });
  release({ outcome: "refused", conv: "ask_aaa" });
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  fireEvent.click(screen.getAllByTestId("ask-recovered-discard")[0]);
  await waitFor(() => expect(screen.queryByTestId("ask-recovered")).toBeNull());
  // the quota is released against the conversation the file was uploaded to
  expect(discardFile).toHaveBeenCalledWith("ask_aaa", ATT);
  // and the operator's newer draft is untouched
  expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe(
    "newer draft",
  );
});


test("sequential refusals are BOTH kept — the queue never overwrites", async () => {
  const releases: ((r: "sent" | "refused") => void)[] = [];
  const onAsk = vi.fn(() => new Promise<{ outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }>((r) => releases.push(r)));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }));
  render(<AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_aaa" />);
  // first send: a file-bearing question, refused while the operator is on a newer draft
  pick(new File(["one"], "first.txt", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "first question" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "second question" } });
  releases[0]!({ outcome: "refused", conv: "ask_aaa" });
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  // second send, also refused while editing a third draft
  fireEvent.click(screen.getByTestId("composer-send"));
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "third draft" } });
  releases[1]!({ outcome: "refused", conv: "ask_aaa" });
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(2));
  // BOTH are present and separately restorable/discardable
  const rows = screen.getAllByTestId("ask-recovered");
  expect(rows[0].textContent).toContain("first question");
  expect(rows[1].textContent).toContain("second question");
  expect(screen.getAllByTestId("ask-recovered-restore")).toHaveLength(2);
  expect(screen.getAllByTestId("ask-recovered-discard")).toHaveLength(2);
});


test("a first-send refusal after lazy creation restores into its own thread", async () => {
  let release: (r: { outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }) => void = () => {};
  const onAsk = vi.fn(() => new Promise((r) => (release = r)));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "ask_created" }));
  render(<AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="ask_created" />);
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "first" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "second" } });
  // the send's aim was the CREATED thread (the outcome carries it): Restore must be offered
  release({ outcome: "refused", conv: "ask_created" });
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  expect(screen.getByTestId("ask-recovered-restore")).toBeTruthy();
});

test("a failure before the message ever started is a refusal — the draft comes back", async () => {
  let release: (r: { outcome: "sent" | "refused" | "canceled" | "uncertain"; conv: string | null }) => void = () => {};
  const onAsk = vi.fn(() => new Promise((r) => (release = r)));
  render(<AskComposer configured onAsk={onAsk} uploadFile={vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }))} />);
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "never left" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  await waitFor(() => expect(onAsk).toHaveBeenCalledTimes(1));
  // the conversation creation failed before the message request: nothing was sent, and the
  // composer has already cleared the draft — the outcome says refused and it comes straight back
  release({ outcome: "refused", conv: null });
  await waitFor(() =>
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe(
      "never left",
    ),
  );
});


test("a send canceled by New conversation never restores into the new thread", async () => {
  let release: (r: {
    outcome: "sent" | "refused" | "canceled";
    conv: string | null;
  }) => void = () => {};
  const onAsk = vi.fn(() => new Promise((r) => (release = r)));
  const uploadFile = vi.fn(async () => ({ attachment: ATT, cid: "aimed_at" }));
  const { rerender } = render(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="aimed_at" />,
  );
  pick(new File(["hello"], "deploy.log", { type: "text/plain" }));
  await waitFor(() => expect(screen.getByText("Ready")).toBeTruthy());
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "old question" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  // the operator starts a NEW conversation while the send is in the air
  rerender(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey={null} />,
  );
  release({ outcome: "canceled", conv: "aimed_at" }); // the switch aborts the watch
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  // the NEW thread's composer is empty — the old question is never restored into it
  expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe("");
  // and the kept row is bound to the thread it was aimed at: no Restore here
  expect(screen.queryByTestId("ask-recovered-restore")).toBeNull();
  expect(screen.getByTestId("ask-recovered-discard")).toBeTruthy();
  // switch back to the original thread and the draft IS restorable
  rerender(
    <AskComposer configured onAsk={onAsk} uploadFile={uploadFile} convKey="aimed_at" />,
  );
  await waitFor(() =>
    expect(screen.getByTestId("ask-recovered-restore")).toBeTruthy(),
  );
});


test("an uncertain outcome keeps the draft but never offers a blind resend", async () => {
  let release: (r: {
    outcome: "sent" | "refused" | "canceled" | "uncertain";
    conv: string | null;
  }) => void = () => {};
  const onAsk = vi.fn(() => new Promise((r) => (release = r)));
  render(
    <AskComposer configured onAsk={onAsk} uploadFile={vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }))} convKey="ask_aaa" />,
  );
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "maybe sent" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  release({ outcome: "uncertain", conv: "ask_aaa" });
  await waitFor(() => expect(screen.getAllByTestId("ask-recovered")).toHaveLength(1));
  // the draft is KEPT but there is no Restore — the turn may exist and must not run twice
  expect(screen.queryByTestId("ask-recovered-restore")).toBeNull();
  expect(screen.getByTestId("ask-recovered").textContent).toContain("may already be sent");
  expect(screen.getByTestId("ask-recovered-discard")).toBeTruthy();
});

test("an admitted turn is never offered as Not sent", async () => {
  let release: (r: {
    outcome: "sent" | "refused" | "canceled" | "uncertain";
    conv: string | null;
  }) => void = () => {};
  const onAsk = vi.fn(() => new Promise((r) => (release = r)));
  render(
    <AskComposer configured onAsk={onAsk} uploadFile={vi.fn(async () => ({ attachment: ATT, cid: "ask_aaa" }))} convKey="ask_aaa" />,
  );
  fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "actually sent" } });
  fireEvent.click(screen.getByTestId("composer-send"));
  // the reconcile finds the turn on the server: it SENT, whatever the watch saw
  release({ outcome: "sent", conv: "ask_aaa" });
  await new Promise((r) => setTimeout(r, 10));
  expect(screen.queryByTestId("ask-recovered")).toBeNull();
  expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe("");
});
