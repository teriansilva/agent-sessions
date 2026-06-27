import { ArrowDown, ArrowRightToLine, ArrowUp, CornerDownLeft, Mic, Paperclip, Pencil, Send, X } from "lucide-react";
import {
  type ClipboardEvent as ReactClipboardEvent,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { api } from "../../lib/api";
import { imageFilesFromData } from "../../lib/clipboardImages";
import { bracketedPaste, KEYSEQ } from "../../lib/termKeys";
import { KeyBar, type KeyAction } from "./KeyBar";
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

/** Debounce for the server-side compose draft (#477): long enough that a burst of typing is
 *  one PUT, short enough that a draft is safe within a beat of pausing. */
const DRAFT_SAVE_DEBOUNCE_MS = 700;

/** Stable signature of a draft's saved content (#477) — compared to skip redundant PUTs.
 *  Only the durable upload path identifies an attachment (name is cosmetic). */
const draftSignature = (text: string, attachments: Attachment[]): string =>
  JSON.stringify({ t: text, a: attachments.map((x) => x.path) });

/** Push-to-talk dictation (#483): the browser's own speech engine, vendor-prefixed on Chromium.
 *  Read lazily (not a module constant) so tests can install a stub on `window` before render and
 *  so an unsupported browser (e.g. Firefox) simply yields `undefined` → the mic chip isn't shown. */
const getSpeechRecognition = (): SpeechRecognitionStatic | undefined =>
  typeof window === "undefined" ? undefined : window.SpeechRecognition ?? window.webkitSpeechRecognition;

/** Append freshly-spoken text to the draft that existed when dictation started (#483), inserting a
 *  single separator only when needed so dictation reads like a continuation of what was typed. */
const joinSpoken = (base: string, spoken: string): string => {
  if (!spoken) return base;
  if (!base) return spoken;
  return /\s$/.test(base) ? base + spoken : `${base} ${spoken}`;
};

/** Imperative handle for parents that want to push files into Compose from outside (e.g.
 *  Terminal forwarding a captured image paste, #157). */
export interface ComposeHandle {
  /** Open Compose (if collapsed) and upload the files as attachment pills — same flow as
   *  a textarea paste, regardless of focus or open state. */
  attachImages: (files: File[]) => void;
}

/** Mobile compose + action bar (the legacy bottom bar). The action row is a single collapsible
 *  group — up / down / return / tab / attach / collapse, overflowing into one "…" menu when narrow
 *  — followed by the mic (audio dictation) and the Send CTA (always last). Keystrokes + the composed
 *  message go to the PTY via `sendInput`. On desktop it stays collapsed by default; image pastes
 *  captured by the parent terminal call `attachImages` to expand it and add pills. */
export const Compose = forwardRef<
  ComposeHandle,
  {
    /** Send one frame to the PTY; returns whether it was actually delivered (socket OPEN). */
    sendInput: (d: string) => boolean;
    /** Id of the current socket (bumped on reconnect) — `send` uses it to avoid an empty submit
     *  when a reconnect splits its clear/paste/Enter frames (#287). Optional for older callers. */
    connEpoch?: () => number;
    /** Whether the text field starts expanded (mobile) or collapsed to the bar (desktop). */
    defaultOpen?: boolean;
    /** Engine-qualified session key (`<engine>:<id>`) this box composes for, used to
     *  persist the draft server-side (#477). `null`/absent ⇒ drafts disabled (a not-yet-real
     *  `new-…` placeholder session has no metadata key — out of scope). */
    sessionId?: string | null;
  }
>(function Compose({ sendInput, connEpoch, defaultOpen = true, sessionId = null }, ref) {
  const [open, setOpen] = useState(defaultOpen);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  // Push-to-talk dictation (#483). At most one active recognizer (`recogRef`); `dictBaseRef` is
  // the draft text present when dictation began. Each result event rebuilds the transcript from the
  // engine's cumulative results list (#487 — no per-event accumulation). `listening` drives the chip.
  const [listening, setListening] = useState(false);
  const recogRef = useRef<SpeechRecognition | null>(null);
  const dictBaseRef = useRef("");

  // Server-side draft (#477) bookkeeping. `dirty` flips true on the first user edit, so a
  // late GET /draft can't clobber text the user already typed; `loadToken` discards a load
  // whose session changed under it; `lastSaved` skips redundant PUTs; `saveTimer` is the
  // debounce; `latest`/`sid` feed the unmount flush without re-running it on every keystroke.
  const dirtyRef = useRef(false);
  const loadTokenRef = useRef(0);
  const lastSavedRef = useRef<string | null>(null);
  const saveTimerRef = useRef<number | undefined>(undefined);
  const latestRef = useRef({ text: "", attachments: [] as Attachment[] });
  const sidRef = useRef<string | null>(sessionId);
  latestRef.current = { text, attachments };
  sidRef.current = sessionId;

  // Persist the current draft now (cancelling any pending debounce). Empty text + no
  // attachments clears it server-side. Skips a no-op when nothing changed since the last save.
  const flushDraft = useCallback(
    (t: string, a: Attachment[]) => {
      if (!sessionId) return;
      const sig = draftSignature(t, a);
      if (sig === lastSavedRef.current) return;
      lastSavedRef.current = sig;
      window.clearTimeout(saveTimerRef.current);
      void api.saveDraft(sessionId, { text: t, attachments: a }).catch(() => {
        // fail-soft: a dropped save just means the draft isn't persisted this beat; the next
        // edit (or the unmount flush) retries. Re-arm so a transient failure isn't sticky.
        lastSavedRef.current = null;
      });
    },
    [sessionId],
  );

  // Clear the draft after a successful send: cancel any pending debounce and PUT an empty
  // draft so a trailing flush can't resurrect what the user just sent (Hermes).
  const clearDraft = () => {
    window.clearTimeout(saveTimerRef.current);
    dirtyRef.current = false;
    flushDraft("", []);
  };

  // Load the saved draft when the session this box composes for changes. The stale-load
  // guard (token + dirty) ensures a slow GET never overwrites newer local input.
  useEffect(() => {
    if (!sessionId) return;
    const token = ++loadTokenRef.current;
    dirtyRef.current = false;
    lastSavedRef.current = null;
    api
      .getDraft(sessionId)
      .then((d) => {
        if (loadTokenRef.current !== token || dirtyRef.current) return; // superseded / user typed
        const atts = d.attachments ?? [];
        lastSavedRef.current = draftSignature(d.text, atts);
        if (d.text || atts.length) {
          setText(d.text);
          setAttachments(atts);
          setOpen(true); // surface the restored draft even if collapsed by default (desktop)
        }
      })
      .catch(() => {
        /* fail-soft: no draft restore (offline / older server) */
      });
  }, [sessionId]);

  // Debounced auto-save on user edits. Gated on `dirty` so the mount-load's setText doesn't
  // echo straight back as a PUT; the signature check skips saves that change nothing.
  useEffect(() => {
    if (!sessionId || !dirtyRef.current) return;
    const sig = draftSignature(text, attachments);
    if (sig === lastSavedRef.current) return;
    window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => flushDraft(text, attachments), DRAFT_SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(saveTimerRef.current);
  }, [text, attachments, sessionId, flushDraft]);

  // Flush a pending draft on unmount (Terminal remounts on session switch) so the last edits
  // aren't lost. `[]` deps → cleanup runs only on real unmount, reading the latest via refs.
  useEffect(() => {
    return () => {
      if (dirtyRef.current && sidRef.current) {
        const { text: t, attachments: a } = latestRef.current;
        if (draftSignature(t, a) !== lastSavedRef.current && sidRef.current) {
          void api.saveDraft(sidRef.current, { text: t, attachments: a }).catch(() => {});
        }
      }
    };
  }, []);

  const grow = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.28))}px`;
  };

  // Stop the active recognizer (tap-to-stop, compose collapse, or teardown). Mark it superseded
  // BEFORE stopping and drop its handlers, so any late callback from this instance is ignored (#483).
  const stopDictation = useCallback(() => {
    const r = recogRef.current;
    recogRef.current = null;
    setListening(false);
    if (r) {
      r.onresult = null;
      r.onerror = null;
      r.onend = null;
      try {
        r.stop();
      } catch {
        /* already stopped */
      }
    }
  }, []);

  // Tap-to-talk start (#483): spin up a fresh recognizer, anchor the current draft, and stream
  // interim + final results into the textarea via the SAME setText + grow + dirty path as typing,
  // so dictated text auto-saves as a draft (#477) and behaves identically to typed text.
  const startDictation = () => {
    const SR = getSpeechRecognition();
    if (!SR) return;
    if (recogRef.current) stopDictation();
    const r = new SR();
    r.continuous = true;
    r.interimResults = true;
    r.lang = (typeof navigator !== "undefined" && navigator.language) || "en-US";
    dictBaseRef.current = text;
    r.onresult = (e) => {
      if (recogRef.current !== r) return; // superseded recognizer — ignore late results
      // Rebuild the WHOLE transcript from the cumulative results list every event, never accumulate
      // per-event (#487): Chrome re-fires onresult many times for the same finalized utterance, so a
      // `finalRef += segment` accumulator typed the phrase ~10×. `e.results` already holds the full
      // session (finalized + current interim), so concatenating + assigning is idempotent.
      let full = "";
      for (let i = 0; i < e.results.length; i++) full += e.results[i][0].transcript;
      dirtyRef.current = true; // dictation is draftable content, just like typing
      setText(joinSpoken(dictBaseRef.current, full.replace(/\s+/g, " ").trim()));
      grow();
    };
    r.onerror = (e) => {
      if (recogRef.current !== r) return;
      recogRef.current = null;
      setListening(false);
      if (e.error && e.error !== "aborted" && e.error !== "no-speech") {
        setNote(
          e.error === "not-allowed" || e.error === "service-not-allowed"
            ? "microphone blocked"
            : "voice input unavailable",
        );
        setTimeout(() => setNote(""), 3000);
      }
    };
    r.onend = () => {
      if (recogRef.current !== r) return; // a fresh recognizer already took over
      recogRef.current = null;
      setListening(false);
    };
    recogRef.current = r;
    setListening(true);
    try {
      r.start();
    } catch {
      // start() throws if it is somehow already running — treat as a failed start.
      recogRef.current = null;
      setListening(false);
    }
  };

  const toggleDictation = () => (listening ? stopDictation() : startDictation());

  // Stop dictation when the box collapses (the mic chip only lives in the open state) and abort it
  // on unmount / session switch, so a recognizer never outlives the compose box it dictates into.
  useEffect(() => {
    if (!open) stopDictation();
  }, [open, stopDictation]);
  useEffect(() => {
    return () => {
      const r = recogRef.current;
      recogRef.current = null;
      if (r) {
        r.onresult = null;
        r.onerror = null;
        r.onend = null;
        try {
          r.abort();
        } catch {
          /* noop */
        }
      }
    };
  }, []);

  const send = () => {
    const savedText = text; // restore exactly these if a (re)paste can't be delivered (#287)
    const savedAttachments = attachments;
    const parts: string[] = [];
    if (text.trim()) parts.push(text.trim());
    for (const a of attachments) parts.push(a.path);
    const msg = parts.join(" ");
    if (!msg) {
      // Empty compose box (no trimmed text, no attachments): act as a bare Return so the Send
      // button — and Enter in the empty field — submit whatever the user typed DIRECTLY into the
      // console (#474). Just a single \r, like a real terminal keypress / the return chip:
      // NO Ctrl-A Ctrl-K clear, NO bracketed paste, NO deferred second Enter (those belong to the
      // content path and would erase or double-submit the console-typed prompt line). If the socket
      // is mid-reconnect (`sendInput` returns false) surface the same note instead of dropping it.
      if (!sendInput(KEYSEQ.enter)) {
        setNote("reconnecting — not sent, try again");
        setTimeout(() => setNote(""), 3000);
      }
      return;
    }
    // A (re)paste that didn't reach the socket means the message isn't there — restore the composer
    // and surface why, and (the caller) must NOT submit a bare Enter (that's the empty-turn bug).
    const abortNotDelivered = () => {
      setText(savedText);
      setAttachments(savedAttachments);
      // #477: the turn wasn't submitted — guarantee the restored content is persisted (it may not
      // have been debounce-saved yet, and a later clear must not win), so a reload / session switch
      // keeps the draft. flushDraft is a no-op when the server already holds this exact content.
      dirtyRef.current = true;
      flushDraft(savedText, savedAttachments);
      setNote("reconnecting — not sent, try again");
      setTimeout(() => setNote(""), 3000);
    };
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
    // Clear the prompt line, then bracketed-paste the message. If the socket is mid-reconnect the
    // paste WON'T deliver (`sendInput` returns false) — do NOT fire a bare Enter later, or it submits
    // an EMPTY turn (#287). Keep the text so the user can resend, and say why.
    sendInput(KEYSEQ.ctrla + KEYSEQ.ctrlk);
    if (!sendInput(bracketedPaste(msg))) {
      abortNotDelivered(); // socket mid-reconnect → not sent; never fire a bare Enter
      return;
    }
    // The Enter is deferred so the agent reads it as a discrete keystroke AFTER the paste-end marker
    // (#180/#226). But a reconnect can land in that gap: the paste went to the now-dead socket while
    // the Enter would hit a FRESH socket that never received it → empty turn. Gate on the socket id:
    // if it changed, re-send clear+paste on the new socket first (the clear prevents any doubling) —
    // and if THAT re-paste also fails (a second reconnect), abort instead of submitting empty.
    const epoch = connEpoch?.();
    setTimeout(() => {
      if (epoch !== undefined && connEpoch?.() !== epoch) {
        sendInput(KEYSEQ.ctrla + KEYSEQ.ctrlk);
        if (!sendInput(bracketedPaste(msg))) {
          abortNotDelivered();
          return;
        }
      }
      // #477/#287: the turn is only actually submitted once this Enter reaches the socket. If it
      // doesn't deliver, the message was NOT sent — restore + re-persist the draft (abort) rather
      // than clearing it. Previously the composer + server draft were cleared synchronously before
      // this point, so a dropped final Enter lost both the message and the draft (Hermes #480).
      if (!sendInput(KEYSEQ.enter)) {
        abortNotDelivered();
        return;
      }
      // Delivered: clear the composer AND the server draft (a just-sent turn must not linger as a
      // draft). clearDraft cancels any pending debounce so a trailing flush can't resurrect it.
      setText("");
      setAttachments([]);
      clearDraft();
      if (taRef.current) taRef.current.style.height = "auto";
    }, enterDelay);
  };

  const uploadFiles = async (files: File[], forceAttachment = false) => {
    if (!files.length) return;
    setNote("uploading…");
    try {
      for (const file of files) {
        const up = await api.upload(file);
        if (open || forceAttachment) {
          dirtyRef.current = true; // #477: an attached image is draftable content
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

  // Hide the mic entirely where the browser has no speech engine (e.g. Firefox) — a natural empty
  // state, no feature flag (#483). Re-read each render so a test stub installed on `window` is seen.
  const speechSupported = !!getSpeechRecognition();

  // The single collapsible group (#487), in order: up, down, return, esc, tab, attach, and — when
  // open — collapse. Everything else (mic, Send) sits inline to the right; no second menu.
  const keyActions: KeyAction[] = [
    { id: "up", aria: "Up", title: "Up", icon: <ArrowUp size={16} />, run: () => sendInput(KEYSEQ.up) },
    { id: "down", aria: "Down", title: "Down", icon: <ArrowDown size={16} />, run: () => sendInput(KEYSEQ.down) },
    { id: "enter", aria: "Return", title: "Return", icon: <CornerDownLeft size={16} />, run: () => sendInput(KEYSEQ.enter) },
    { id: "esc", aria: "Escape", title: "Escape", text: "esc", run: () => sendInput(KEYSEQ.esc) },
    { id: "tab", aria: "Tab", title: "Tab", icon: <ArrowRightToLine size={16} />, run: () => sendInput(KEYSEQ.tab) },
    {
      id: "attach",
      aria: "Attach file",
      title: "Attach an image or file",
      icon: <Paperclip size={16} />,
      run: () => fileRef.current?.click(),
    },
    ...(open
      ? [
          {
            id: "close",
            aria: "Collapse compose box",
            title: "Collapse",
            icon: <X size={16} />,
            run: () => setOpen(false),
          },
        ]
      : []),
  ];

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
                    onClick={() => {
                      dirtyRef.current = true; // #477: removing a pill edits the draft
                      setAttachments((prev) => prev.filter((_, j) => j !== i));
                    }}
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
              dirtyRef.current = true; // #477: user edit → eligible for draft auto-save
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
        <KeyBar actions={keyActions} />
        <span className={styles.spacer}>{note}</span>
        {open && speechSupported && (
          <button
            type="button"
            className={listening ? `${styles.mic} ${styles.micOn}` : styles.mic}
            aria-label={listening ? "Stop voice input" : "Start voice input"}
            aria-pressed={listening}
            title={listening ? "Stop dictation" : "Dictate (voice to text)"}
            onClick={toggleDictation}
          >
            <Mic size={16} />
          </button>
        )}
        {open ? (
          <button type="button" className={`${styles.send} shine`} title="Send + Enter" onClick={send}>
            <Send size={15} />
            Send
          </button>
        ) : (
          // Collapsed (desktop default): the only inline control is "compose" (open). Collapse lives
          // in the key group once open.
          <button
            type="button"
            className={styles.toggle}
            aria-label="Open compose box"
            title="Compose"
            onClick={() => setOpen(true)}
          >
            <Pencil size={16} />
          </button>
        )}
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
