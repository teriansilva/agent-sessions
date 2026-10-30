/** Ask's field (#1171) — one box. Its only production mount is the HQ sidebar (`AskConsole` in
 *  `AskSidebar`; the page mount it once had is gone — #1775), so its box never exceeds the
 *  sidebar's 440px. Since #1707 it also stages shared files.
 *
 *  **Enter sends; Shift+Enter is a new line.** It is a chat, and the operator asked for the chat
 *  default. Ctrl/⌘+Enter still sends too, so a hand trained on the old shortcut is not surprised.
 *  An Enter that finishes an IME composition (`isComposing`, or the legacy keyCode 229) is the
 *  composition's own, never a send — otherwise confirming a Japanese word would submit half a
 *  question.
 *
 *  **Files (#1707).** **Attach** takes any type; **Camera** is `accept="image/*" capture="environment"`
 *  — capture goes through the OS picker, so there is no `getUserMedia` permission prompt to manage,
 *  and desktop ignores `capture` and gets a plain picker. A cancelled or denied picker is a silent
 *  no-op: no pill, no error. Staged files are pills above the box (uploading / ready / named
 *  failure) with a labeled, focusable remove button. **Send is blocked while a file uploads or a
 *  failure is unresolved** (#1605's rule, both halves) and a blocked attempt never fires later on
 *  its own — removing the failed pill is the only unblock. A file-only message is a message: with
 *  something ready and no text, Send sends the files.
 *
 *  The box, its foot and its Send are the console's primitives (`mission.module.css`) and the
 *  session pane's Send (#967), as before — one box, drawn one way, wherever it appears. Attach,
 *  Camera and the row actions are the shared `ui/actionButton.module.css` plates (NIGHTJAR §6).
 */
import { Camera, Paperclip, Send } from "lucide-react";
import { useEffect, useRef, useState, type Ref } from "react";

import type { AskAttachment } from "../../types/api";
import compose from "../terminal/Compose.module.css";
import styles from "../pulse/mission.module.css";
import a from "./AskConsole.module.css";
import { secretByDefault } from "./secretFiles";

/** A staged pill's own identity (never shown, never sent). */
const newLocal = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

/** One staged file, from picker to claimable id. */
export interface StagedFile {
  /** The pill's identity (a removed pill must not confuse an in-flight upload). */
  local: string;
  name: string;
  size: number;
  status: "uploading" | "ready" | "failed";
  /** The server's descriptor once `ready`. */
  attachment?: AskAttachment;
  /** The conversation the file was uploaded to — a discard always names THIS, never the
   *  conversation the operator happens to be looking at later (#1720 round 3). */
  cid?: string;
  /** The named reason once `failed` — never a silent drop. */
  why?: string;
  /** Share it as a SECRET (#1786): HQ sees only its name and size and can only propose moving
   *  it into the Library. On by default for key and certificate files. */
  secret: boolean;
  /** The picked file, kept to upload it again when the Secret switch changes. */
  file?: File;
}

export function AskComposer({
  configured,
  busy = false,
  onAsk,
  inputRef,
  uploadFile,
  discardFile,
  cleanupPending = 0,
  convKey = null,
  threadKey = 0,
  onDraftChange,
}: {
  /** False when no AI endpoint is configured: the field is disabled and says why. */
  configured: boolean;
  /** A question is in flight: typing stays possible, sending does not. */
  busy?: boolean;
  onAsk: (
    question: string,
    attachments: AskAttachment[],
  ) => void | Promise<{
    outcome: "sent" | "refused" | "canceled" | "uncertain";
    conv: string | null;
  }>;
  /** Files removed but whose server-side DELETE has not landed yet (#1720 round 5): said out
   *  loud instead of silently promising the quota as free. */
  cleanupPending?: number;
  inputRef?: Ref<HTMLTextAreaElement>;
  /** Stage one file server-side, answering the descriptor AND the conversation it landed in. */
  uploadFile?: (file: File, secret: boolean) => Promise<{ attachment: AskAttachment; cid: string }>;
  /** Take back a staged file the operator removed (or whose pill went away mid-upload) FROM THE
   *  CONVERSATION IT WAS UPLOADED TO, so the server's four-file quota frees at once (#1720). */
  /** Takes a staged file back; its promise settles once the server has let it go (the Secret
   *  switch waits for that before uploading the replacement, so a full set never goes over the
   *  staged-file limit, #1786). */
  discardFile?: (cid: string, attachment: AskAttachment) => void | Promise<void>;
  /** The conversation on screen. Staged files belong to the one they were uploaded to; switching
   *  to a different conversation clears them rather than sending them somewhere else (#1707). */
  convKey?: string | null;
  /** Whether unsent text or a staged file is in the box (#1776: the sidebar's Open as page waits
   *  for it, so a draft is never lost or silently carried across). */
  onDraftChange?: (hasDraft: boolean) => void;
  /** Moves on every SWITCH of the thread on screen (a pick, New, Back/Forward, a delete) — never
   *  on the first send creating a conversation (#1776, Hermes review 7393). */
  threadKey?: number;
}) {
  const [text, setText] = useState("");
  // UNSENT TEXT BELONGS TO ITS CONVERSATION (#1776, Hermes review 7393): a switch keeps the text
  // typed in the thread being left under that conversation (`""` = a thread not created yet) and
  // shows the next one's own draft — never A's words in B's box, where Send would deliver them.
  // Adjusted while rendering, as React prescribes for state derived from a prop change.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [thread, setThread] = useState({ key: threadKey, conv: convKey });
  if (thread.key !== threadKey) {
    const kept = { ...drafts, [thread.conv ?? ""]: text };
    setDrafts(kept);
    setText(kept[convKey ?? ""] ?? "");
    setThread({ key: threadKey, conv: convKey });
  } else if (thread.conv !== convKey) {
    setThread({ key: threadKey, conv: convKey }); // created by its first send: the same thread
  }
  const [staged, setStaged] = useState<StagedFile[]>([]);
  const attachRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);

  const unresolved = staged.some((s) => s.status !== "ready");
  const ready = staged.filter((s) => s.status === "ready" && s.attachment);
  const canSend =
    configured && !busy && !unresolved && (text.trim() !== "" || ready.length > 0);

  const hasDraft = text.trim() !== "" || staged.length > 0;
  useEffect(() => {
    onDraftChange?.(hasDraft);
  }, [hasDraft, onDraftChange]);

  const stagedRef = useRef<StagedFile[]>([]);
  // The ref tracks the state SYNCHRONOUSLY inside the updaters (idempotent — the same value
  // lands twice under StrictMode) as well as in an effect: `remove` reads it in its click
  // handler, and an effect-only sync let a fast click beat it under load (#1720 round 7).
  const setStagedTracked = (
    update: StagedFile[] | ((prev: StagedFile[]) => StagedFile[]),
  ) =>
    setStaged((prev) => {
      const next = typeof update === "function" ? update(prev) : update;
      stagedRef.current = next;
      return next;
    });
  useEffect(() => {
    stagedRef.current = staged;
  }, [staged]);
  /** Bumped by every operator edit (#1720 round 6). A refusal may only restore a draft that is
   *  still the one that was sent — never one the operator has already moved past. */
  const draftRev = useRef(0);
  /** Sent drafts the server refused while the operator was already on a newer one (#1720
   *  rounds 7–8): NEVER dropped — a QUEUE of them, each visible with Restore and Discard, so
   *  every question and its files survive and the quota they hold can be released deliberately.
   *  A second refusal ADDS; it never overwrites the first. */
  const [recovered, setRecovered] = useState<
    { text: string; items: StagedFile[]; conv: string | null; safe: boolean }[]
  >([]);

  // A switch (or New conversation) drops what was staged elsewhere — and discards the server
  // blobs with it, so the quota is not held for 24 h by files nobody can see. The lazy-create
  // of a first conversation is NOT a switch — the key goes null → id while the operator stays.
  const prevKey = useRef(convKey);
  useEffect(() => {
    if (prevKey.current && convKey !== prevKey.current) {
      for (const s of stagedRef.current)
        if (s.attachment && s.cid) discardFile?.(s.cid, s.attachment);
      setStagedTracked([]);
    }
    prevKey.current = convKey;
  }, [convKey, discardFile]);

  const send = () => {
    if (!canSend) return; // the #1605 guard: blocked attempts send nothing, now or later
    const q = text.trim();
    const atts = ready.map((s) => s.attachment!);
    const carried = stagedRef.current.filter((s) => s.status === "ready");
    const sentFrom = prevKey.current; // the conversation this draft belongs to
    const sentRev = draftRev.current; // the draft revision that was sent
    setText("");
    setStagedTracked([]);
    // #1720 round 5: the draft is only FORGOTTEN when the server accepts. A confirmed refusal
    // (nothing written — 409/422/503) puts the text and the same staged files back so the
    // operator can fix and resend; an uncertain outcome keeps them gone rather than risk a
    // duplicate turn. Round 6: never restore OVER a newer draft — if the operator typed or
    // staged anything while the request was in flight, those edits win and the refused draft
    // is simply gone (nothing was written server-side; its staged files were real, and the
    // pills the operator still sees stay theirs).
    void Promise.resolve(onAsk(q, atts)).then((res) => {
      if (!res) return;
      if (res.outcome === "sent") return;
      if (res.outcome === "refused" && res.conv === convKey && draftRev.current === sentRev) {
        // Untouched, same thread, confirmed nothing written: give the draft straight back.
        setText(q);
        setStagedTracked(carried);
        return;
      }
      // Anything else — the operator moved on to a newer draft, the refusal landed on another
      // thread, or the send was CANCELED by a switch/New (#1720 round 12) — is KEPT as a
      // recovery row bound to the thread the send was AIMED at. It is never auto-restored:
      // a canceled old-thread question must leave the new thread's composer empty. A second
      // one JOINS the queue — it never replaces the first (#1720 round 8).
      if (q || carried.length)
        setRecovered((prev) => [
          ...prev,
          {
            text: q,
            items: carried,
            conv: res.conv ?? sentFrom,
            // #1720 round 13: an UNCERTAIN outcome (the watch died and the server could not be
            // asked) keeps the draft but never offers a blind resend — the turn may exist.
            safe: res.outcome !== "uncertain",
          },
        ]);
    });
  };

  const restoreRecovered = (i: number) => {
    const r = recovered[i];
    if (!r) return;
    draftRev.current += 1;
    setRecovered((prev) => prev.filter((_, j) => j !== i));
    // Merged, never overwritten: the newer text and pills stay, the refused ones join them.
    setText((cur) => (cur.trim() ? `${cur}\n${r.text}`.trim() : r.text));
    setStagedTracked((cur) => [...r.items, ...cur]);
  };

  const discardRecovered = (i: number) => {
    const r = recovered[i];
    if (!r) return;
    setRecovered((prev) => prev.filter((_, j) => j !== i));
    for (const s of r.items) if (s.attachment && s.cid) discardFile?.(s.cid, s.attachment);
  };

  /** Upload ``file`` for the pill ``local`` with its Secret flag; a pill gone meanwhile hands its
   *  blob straight back. A flag changed meanwhile wins: this answer is dropped and discarded. */
  const upload = (local: string, file: File, secret: boolean) => {
    if (!uploadFile) return;
    const current = (x: StagedFile) => x.local === local && x.secret === secret;
    uploadFile(file, secret).then(
      ({ attachment, cid }) => {
        // The pill may be gone (Remove mid-upload, or a conversation switch), or its Secret
        // switch moved since: the just-landed blob goes back to the server at once.
        if (!stagedRef.current.some(current)) {
          discardFile?.(cid, attachment);
          return;
        }
        setStagedTracked((prev) =>
          prev.map((s) => (current(s) ? { ...s, status: "ready", attachment, cid } : s)),
        );
      },
      () =>
        // A named failure, never a silent drop: the pill stays and blocks Send until removed.
        setStagedTracked((prev) =>
          prev.map((s) =>
            current(s) ? { ...s, status: "failed", why: "Couldn't read that file" } : s,
          ),
        ),
    );
  };

  const stage = (files: FileList | null) => {
    if (!files?.length || !uploadFile) return;
    draftRev.current += 1; // a new staged file is an edit the restore must not overwrite
    for (const file of Array.from(files)) {
      const local = newLocal();
      const name = file.name || "pasted";
      const secret = secretByDefault(name);
      setStagedTracked((prev) => [
        ...prev,
        { local, name, size: file.size, status: "uploading", secret, file },
      ]);
      upload(local, file, secret);
    }
  };

  /** Flip a pill's Secret switch: the file is shared again with the new flag (the old upload is
   *  taken back), because the server fixes the flag at upload. */
  const toggleSecret = async (local: string) => {
    const s = stagedRef.current.find((x) => x.local === local);
    if (!s || !s.file || s.status === "uploading") return;
    draftRev.current += 1;
    const secret = !s.secret;
    const old = s.attachment && s.cid ? { cid: s.cid, attachment: s.attachment } : null;
    setStagedTracked((prev) =>
      prev.map((x) =>
        x.local === local
          ? {
              ...x,
              secret,
              status: "uploading",
              attachment: undefined,
              cid: undefined,
              why: old ? "Removing the previous copy…" : undefined,
            }
          : x,
      ),
    );
    // The old copy is let go FIRST: with four files staged, a fifth upload before the discard
    // lands would be refused by the server's staged-file limit.
    if (old) {
      // The replacement waits until the old copy is CONFIRMED gone (Hermes on #1816): uploading
      // first could be refused by the staged-file limit with four files waiting. The pill says
      // what it waits for, Send stays blocked meanwhile, and Remove still works.
      await Promise.resolve(discardFile?.(old.cid, old.attachment)).catch(() => {});
    }
    if (!stagedRef.current.some((x) => x.local === local && x.secret === secret)) return;
    setStagedTracked((prev) =>
      prev.map((x) => (x.local === local && x.secret === secret ? { ...x, why: undefined } : x)),
    );
    upload(local, s.file, secret);
  };

  const remove = (local: string) => {
    draftRev.current += 1;
    const gone = stagedRef.current.find((s) => s.local === local);
    if (gone?.attachment && gone.cid) discardFile?.(gone.cid, gone.attachment);
    setStagedTracked((prev) => prev.filter((s) => s.local !== local));
  };

  return (
    <form
      className={`${styles.composerBox} ${a.box}`}
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
      data-testid="ask-form"
    >
      {/* The pickers: any type (Attach) and image capture (Camera). Hidden — the plates open
          them. A cancelled picker changes nothing, which is the whole error handling. */}
      <input
        ref={attachRef}
        type="file"
        multiple
        hidden
        data-testid="ask-attach-input"
        onChange={(e) => {
          stage(e.target.files);
          e.target.value = ""; // the same file can be re-picked after a remove
        }}
      />
      <input
        ref={cameraRef}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        data-testid="ask-camera-input"
        onChange={(e) => {
          stage(e.target.files);
          e.target.value = "";
        }}
      />
      {recovered.map((r, i) => (
        <div className={a.recovered} data-testid="ask-recovered" role="status" key={i}>
          <span className={a.recoveredText}>
            Not sent: {r.text.slice(0, 80) || `${r.items.length} file(s)`}
            {r.items.length ? ` · ${r.items.length} file(s)` : ""}
          </span>
          <span className={a.recoveredActions}>
            {r.safe && r.conv !== null && r.conv === convKey ? (
              <button
                type="button"
                className={a.recoveredBtn}
                data-testid="ask-recovered-restore"
                onClick={() => restoreRecovered(i)}
              >
                Restore
              </button>
            ) : (
              <span className={a.cleanupWhy}>
                {r.safe ? "belongs to another conversation" : "may already be sent — kept, not resendable"}
              </span>
            )}
            <button
              type="button"
              className={a.recoveredBtn}
              data-testid="ask-recovered-discard"
              onClick={() => discardRecovered(i)}
            >
              Discard
            </button>
          </span>
        </div>
      ))}
      {cleanupPending > 0 ? (
        <div className={a.cleanup} data-testid="ask-cleanup-pending" role="status">
          Freeing {cleanupPending} removed file{cleanupPending === 1 ? "" : "s"}…
          <span className={a.cleanupWhy}> still on the server until a turn lets go</span>
        </div>
      ) : null}
      {staged.length > 0 ? (
        <div className={a.pills} data-testid="ask-staged">
          {staged.map((s) => (
            <div
              key={s.local}
              className={`${a.pill} ${s.status === "failed" ? a.pillFailed : ""}`}
              data-testid="ask-staged-pill"
            >
              <span className={`${styles.led} ${s.status === "ready" ? "up" : s.status === "failed" ? "down" : "draft"}`} aria-hidden="true" />
              <span className={a.pillName}>{s.name}</span>
              <span className={a.pillMeta}>{formatSize(s.size)}</span>
              <button
                type="button"
                role="switch"
                aria-checked={s.secret}
                aria-label={`Share ${s.name} as a secret`}
                title="A secret is never shown to HQ; HQ can only propose storing it in the Library"
                className={`${a.pillSecret} btn-compact`}
                disabled={s.status === "uploading" || !s.file}
                onClick={() => void toggleSecret(s.local)}
                data-testid="ask-staged-secret"
              >
                Secret · {s.secret ? "on" : "off"}
              </button>
              <span
                className={s.status === "failed" ? a.pillWhy : a.pillState}
                data-testid="ask-staged-state"
              >
                {s.status === "uploading"
                  ? (s.why ?? "Uploading…")
                  : s.status === "failed"
                    ? s.why
                    : "Ready"}
              </span>
              <button
                type="button"
                className={`${a.pillX} btn-compact`}
                aria-label={`Remove ${s.name}`}
                data-testid="ask-staged-remove"
                onClick={() => remove(s.local)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <textarea
        ref={inputRef}
        className={`${styles.composerInput} ${styles.boxInput}`}
        rows={1}
        value={text}
        onChange={(e) => {
          draftRev.current += 1;
          setText(e.target.value);
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter" || e.shiftKey) return;
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          e.preventDefault();
          e.currentTarget.form?.requestSubmit();
        }}
        aria-keyshortcuts="Enter"
        disabled={!configured}
        placeholder={
          configured
            ? "Ask about your sessions and missions…"
            : "Needs an AI endpoint"
        }
        aria-label="Ask about your past work"
        data-testid="composer-input"
      />
      <div className={styles.composerFoot} data-testid="composer-foot">
        <div className={styles.footLead}>
          <button
            type="button"
            className={a.iconBtn}
            aria-label="Attach file"
            title="Attach file"
            disabled={!configured || !uploadFile}
            data-testid="ask-attach"
            onClick={() => attachRef.current?.click()}
          >
            <Paperclip size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={a.iconBtn}
            aria-label="Take photo"
            title="Take photo"
            disabled={!configured || !uploadFile}
            data-testid="ask-camera"
            onClick={() => cameraRef.current?.click()}
          >
            <Camera size={16} aria-hidden="true" />
          </button>
          {/* The shortcut, for the eye. The textarea's `aria-keyshortcuts` is its accessible
              form. */}
          <span className={`${styles.footHint} ${a.hint}`} aria-hidden="true" data-testid="composer-hint">
            Enter to send
            <span className={a.hintMore}> · Shift + Enter new line</span>
          </span>
        </div>
        <div className={styles.footTrail}>
          <span className={styles.footSpacer} aria-hidden="true" />
          <button
            type="submit"
            className={`${compose.send} shine`}
            disabled={!canSend}
            data-testid="composer-send"
          >
            <Send size={15} aria-hidden="true" />
            Send
          </button>
        </div>
      </div>
    </form>
  );
}

function formatSize(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}
