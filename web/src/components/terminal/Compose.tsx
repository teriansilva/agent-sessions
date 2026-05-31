import { Pencil, Send, X } from "lucide-react";
import {
  type ClipboardEvent as ReactClipboardEvent,
  forwardRef,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { api } from "../../lib/api";
import { imageFilesFromData } from "../../lib/clipboardImages";
import { bracketedPaste, KEYSEQ } from "../../lib/termKeys";
import { KeyBar } from "./KeyBar";
import styles from "./Compose.module.css";

interface Attachment {
  name: string;
  path: string;
}

/** The composed message is delivered as a bracketed paste followed by a separate Enter. The
 *  Enter must land in a LATER task than the paste, or the agent (e.g. Claude Code) can read the
 *  trailing \r as still inside the bracketed-paste buffer and leave the prompt typed but
 *  unsubmitted — the "I had to press Enter twice" bug. #180 split the \r into its own WS frame
 *  and #197 deferred it for attachments, but a same-tick text send still raced on slower/mobile
 *  links. So ALWAYS defer the Enter; attachments (async image-path ingestion) need a longer beat.
 *  ~60ms is imperceptible but reliably separates the PTY writes. */
const ENTER_DELAY_MS = 60;
const ENTER_DELAY_AFTER_ATTACHMENT_MS = 120;

/** Imperative handle for parents that want to push files into Compose from outside (e.g.
 *  Terminal forwarding a captured image paste, #157). */
export interface ComposeHandle {
  /** Open Compose (if collapsed) and upload the files as attachment pills — same flow as
   *  a textarea paste, regardless of focus or open state. */
  attachImages: (files: File[]) => void;
}

/** Mobile compose + action bar (the legacy bottom bar): nav/control keys, file attach,
 *  copy, and a collapsible autocomplete-safe text field. Keystrokes + the composed
 *  message go to the PTY via `sendInput`. On desktop it stays collapsed by default; image
 *  pastes captured by the parent terminal call `attachImages` to expand it and add pills. */
export const Compose = forwardRef<
  ComposeHandle,
  {
    sendInput: (d: string) => void;
    onCopy: () => void;
    /** Whether the text field starts expanded (mobile) or collapsed to the bar (desktop). */
    defaultOpen?: boolean;
  }
>(function Compose({ sendInput, onCopy, defaultOpen = true }, ref) {
  const [open, setOpen] = useState(defaultOpen);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const grow = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.28))}px`;
  };

  const send = () => {
    const parts: string[] = [];
    if (text.trim()) parts.push(text.trim());
    for (const a of attachments) parts.push(a.path);
    const msg = parts.join(" ");
    if (!msg) return;
    // Clear the prompt line (Ctrl-A, Ctrl-K) so leftover input doesn't mix in, then bracketed-
    // paste the message, then submit the Enter as a SEPARATE, DEFERRED frame.
    // (#180) The original form bundled ``bracketedPaste(msg) + KEYSEQ.enter`` into one WS frame
    // → one PTY write → one read, so the agent could read the trailing ``\r`` as still inside the
    // bracketed-paste buffer and leave the prompt typed but unsubmitted. Splitting the ``\r``
    // into its own frame made it a discrete keystroke after the paste-end marker.
    // (#197) Even split, an attachment's async image-path ingestion still raced an immediate
    // Enter and dropped it, so the Enter was deferred for attachments.
    // (#226) A same-tick text send still raced on slower/mobile links — you had to press Enter
    // twice. So ALWAYS defer the Enter into a later task: text uses ENTER_DELAY_MS, attachments
    // the longer ENTER_DELAY_AFTER_ATTACHMENT_MS.
    const enterDelay = attachments.length > 0 ? ENTER_DELAY_AFTER_ATTACHMENT_MS : ENTER_DELAY_MS;
    sendInput(KEYSEQ.ctrla + KEYSEQ.ctrlk);
    sendInput(bracketedPaste(msg));
    setTimeout(() => sendInput(KEYSEQ.enter), enterDelay);
    setText("");
    setAttachments([]);
    if (taRef.current) taRef.current.style.height = "auto";
  };

  const uploadFiles = async (files: File[], forceAttachment = false) => {
    if (!files.length) return;
    setNote("uploading…");
    try {
      for (const file of files) {
        const up = await api.upload(file);
        if (open || forceAttachment) {
          setAttachments((prev) => [...prev, { name: up.name, path: up.path }]);
        } else {
          sendInput(bracketedPaste(up.path) + " ");
        }
      }
      setNote("");
    } catch {
      setNote("upload failed");
      setTimeout(() => setNote(""), 3000);
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  const pickFiles = (files: FileList | null) => uploadFiles(Array.from(files ?? []));

  // External path (#157): the parent terminal forwards a captured image paste here. Open
  // Compose if it was collapsed (desktop default) and always upload as an attachment pill,
  // so the user actually sees the screenshot landed.
  useImperativeHandle(ref, () => ({
    attachImages: (files: File[]) => {
      if (!files.length) return;
      if (!open) setOpen(true);
      void uploadFiles(files, true);
    },
  }));

  // Paste an image (screenshot) into the compose box → upload it like an attachment
  // instead of letting the textarea swallow the (empty) text. Plain-text paste is left
  // to the textarea (#135).
  const onPaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const images = imageFilesFromData(e.clipboardData);
    if (!images.length) return;
    e.preventDefault();
    void uploadFiles(images);
  };

  return (
    <div className={styles.compose}>
      {open && (
        <div className={styles.chHead}>
          <span className="hud-tag">
            <span className="hud-led up" aria-hidden="true" /> CH OPEN
          </span>
          <span className={styles.hints}>↵ SEND · ⇧↵ NEWLINE</span>
        </div>
      )}
      {open && (
        <div className={styles.fields}>
          {attachments.length > 0 && (
            <div className={styles.pills}>
              {attachments.map((a, i) => (
                <span key={a.path} className={styles.pill} title={a.path}>
                  <span className={styles.pn}>{a.name}</span>
                  <button
                    type="button"
                    aria-label="Remove attachment"
                    onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            className={styles.textarea}
            rows={2}
            value={text}
            placeholder="Type here — Enter sends, Shift+Enter = newline."
            onChange={(e) => {
              setText(e.target.value);
              grow();
            }}
            onPaste={onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
        </div>
      )}

      <div className={styles.row}>
        <KeyBar sendInput={sendInput} onCopy={onCopy} onAttach={() => fileRef.current?.click()} />
        <span className={styles.spacer}>{note}</span>
        {open && (
          <button type="button" className={`${styles.send} shine`} title="Send + Enter" onClick={send}>
            <Send size={15} />
            Send
          </button>
        )}
        <button
          type="button"
          className={styles.toggle}
          aria-label={open ? "Collapse compose box" : "Open compose box"}
          title={open ? "Collapse" : "Compose"}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? <X size={16} /> : <Pencil size={16} />}
        </button>
      </div>

      <input
        ref={fileRef}
        type="file"
        hidden
        multiple
        onChange={(e) => void pickFiles(e.target.files)}
      />
    </div>
  );
});
