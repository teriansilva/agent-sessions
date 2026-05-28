import {
  ArrowDown,
  ArrowRightToLine,
  ArrowUp,
  Copy,
  CornerDownLeft,
  Paperclip,
  Pencil,
  Send,
  Square,
  X,
} from "lucide-react";
import {
  type ClipboardEvent as ReactClipboardEvent,
  forwardRef,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { api } from "../../lib/api";
import { imageFilesFromData } from "../../lib/clipboardImages";
import { bracketedPaste, KEYSEQ, type KeyName } from "../../lib/termKeys";
import styles from "./Compose.module.css";

interface Attachment {
  name: string;
  path: string;
}

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

  const key = (name: KeyName) => sendInput(KEYSEQ[name]);

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
    // Clear the prompt line (Ctrl-A, Ctrl-K) so leftover input doesn't mix in, then
    // paste the message in one shot + Enter.
    sendInput(KEYSEQ.ctrla + KEYSEQ.ctrlk);
    sendInput(bracketedPaste(msg) + KEYSEQ.enter);
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
        <div className={styles.keys}>
          <button type="button" aria-label="Up" title="Up" onClick={() => key("up")}>
            <ArrowUp size={16} />
          </button>
          <button type="button" aria-label="Down" title="Down" onClick={() => key("down")}>
            <ArrowDown size={16} />
          </button>
          <button type="button" aria-label="Enter" title="Enter" onClick={() => key("enter")}>
            <CornerDownLeft size={16} />
          </button>
          <button
            type="button"
            className={styles.txt}
            aria-label="Escape"
            title="Escape"
            onClick={() => key("esc")}
          >
            esc
          </button>
          <button type="button" aria-label="Tab" title="Tab" onClick={() => key("tab")}>
            <ArrowRightToLine size={16} />
          </button>
          <button
            type="button"
            aria-label="Interrupt (send Ctrl-C)"
            title="Send Ctrl-C (interrupt)"
            onClick={() => key("ctrlc")}
          >
            <Square size={14} fill="currentColor" />
          </button>
          <button
            type="button"
            aria-label="Attach file"
            title="Attach an image or file"
            onClick={() => fileRef.current?.click()}
          >
            <Paperclip size={16} />
          </button>
          <button type="button" aria-label="Copy" title="Copy selection" onClick={onCopy}>
            <Copy size={16} />
          </button>
        </div>
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
