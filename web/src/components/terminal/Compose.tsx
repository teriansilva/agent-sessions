import {
  ArrowDown,
  ArrowRightToLine,
  ArrowUp,
  CornerDownLeft,
  History,
  Mic,
  Paperclip,
  Pencil,
  Send,
  X,
} from "lucide-react";
import {
  type ClipboardEvent as ReactClipboardEvent,
  type PointerEvent as ReactPointerEvent,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { api } from "../../lib/api";
import {
  imageFilesFromAsyncClipboard,
  imageFilesFromData,
} from "../../lib/clipboardImages";
import {
  type SentMessage,
  appendSent,
  confirmSent,
  readSent,
} from "../../lib/sentHistory";
import { SentMessagesModal } from "./SentMessagesModal";
import {
  assembleSpoken,
  isSpaceDelimitedLang,
  type SpokenSegment,
} from "../../lib/dictation";
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

/** Fresh-launch readiness hold (#533): how long a first Send waits for the booting agent's
 *  input to come live before giving up (keeping the draft + surfacing "not sent"). Generous —
 *  a loaded host has been observed taking ~10s to first paint. */
const READY_WAIT_MS = 20_000;

/** Debounce for the server-side compose draft (#477): long enough that a burst of typing is
 *  one PUT, short enough that a draft is safe within a beat of pausing. */
const DRAFT_SAVE_DEBOUNCE_MS = 700;

/** Stable signature of a draft's saved content (#477) — compared to skip redundant PUTs.
 *  Only the durable upload path identifies an attachment (name is cosmetic). */
const draftSignature = (text: string, attachments: Attachment[]): string =>
  JSON.stringify({ t: text, a: attachments.map((x) => x.path) });

/** Push-to-talk dictation (#483, a real press-and-hold since #738): the browser's own speech
 *  engine, vendor-prefixed on Chromium.
 *  Read lazily (not a module constant) so tests can install a stub on `window` before render and
 *  so an unsupported browser (e.g. Firefox) simply yields `undefined` → the mic chip isn't shown. */
const getSpeechRecognition = (): SpeechRecognitionStatic | undefined =>
  typeof window === "undefined"
    ? undefined
    : (window.SpeechRecognition ?? window.webkitSpeechRecognition);

/** Append freshly-spoken text to the draft that existed when dictation started (#483), inserting a
 *  single separator only when needed so dictation reads like a continuation of what was typed. */
const joinSpoken = (base: string, spoken: string): string => {
  if (!spoken) return base;
  if (!base) return spoken;
  return /\s$/.test(base) ? base + spoken : `${base} ${spoken}`;
};

/** Dictation outlives the ENGINE's session, not just the user's hold (#736).
 *
 *  A `SpeechRecognition` session is not a recording the user controls — the engine ends it on its
 *  own: Chrome's endpointer gives up on a silent stretch (`no-speech` → `end`), the service caps a
 *  connection, and the Android fallback recognizer (`continuous = false`, see `beginRecognition`)
 *  ends after every single utterance by definition. Each of those ended the whole dictation, which
 *  is why speech "recorded a part and then stopped". So an ended session re-arms a fresh recognizer
 *  while the user is still holding the mic open, and two bounds keep that from becoming an
 *  unbounded mic hold or a hot restart loop:
 *
 *    • `DICTATION_IDLE_STOP_MS` — silence, measured from the last words actually heard, after which
 *      re-arming stops. A hold that goes silent (a wedged pointer, a stuck key) gets the mic
 *      released rather than an indefinitely lit chip.
 *    • `DICTATION_DEAD_START_LIMIT` consecutive sessions that end within `DICTATION_DEAD_START_MS`
 *      of starting WITHOUT hearing a word — an engine refusing to run, which a plain re-arm would
 *      spin on. Any session that hears speech clears the count, so ordinary use never approaches it.
 *
 *  Both bounds fail toward stopping, never toward a silent spin. */
export const DICTATION_IDLE_STOP_MS = 60_000;
const DICTATION_DEAD_START_MS = 400;
const DICTATION_DEAD_START_LIMIT = 3;

/** How long a RELEASE waits for the engine to deliver its last result before tearing down anyway
 *  (#738). Releasing calls `stop()`, which ends capture but still owes us what was already heard —
 *  so the recognizer is kept alive, and its `onend` normally completes the teardown well inside this
 *  window. The timer only matters for an engine that never reports the end: without it the chip
 *  would sit in `finalizing` forever, refusing the next hold. */
export const DICTATION_FINALIZE_MS = 3_000;

/** Human-readable note for a dictation failure. Maps the SpeechRecognition `error` codes AND the
 *  DOMException `name`s that getUserMedia rejects with to something actionable — and, crucially,
 *  the default arm echoes the raw code so an unmapped failure names itself instead of hiding behind
 *  a generic "microphone blocked" (the string that made the #659 header bug so hard to pin down). */
const micErrorNote = (code: string): string => {
  switch (code) {
    case "not-allowed":
    case "NotAllowedError":
    case "SecurityError":
      return "mic blocked — allow microphone for this site in your browser settings";
    case "service-not-allowed":
      return "speech recognition unavailable on this device/browser";
    case "audio-capture":
    case "NotFoundError":
      return "no microphone found";
    case "network":
      return "voice input needs a network connection";
    case "language-not-supported":
      return "dictation language not supported";
    default:
      return `voice input error: ${code}`;
  }
};

/** The DOMException name (or a fallback) a getUserMedia rejection carries, for `micErrorNote`. */
const gumErrorCode = (err: unknown): string => {
  if (err && typeof err === "object" && "name" in err) {
    const name = (err as { name?: unknown }).name;
    if (typeof name === "string") return name;
  }
  return "NotAllowedError";
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
    /** Fresh-launch readiness gate (#533): `true` when the agent's input is live (the common
     *  case — delivery proceeds synchronously, sequencing unchanged), else a promise resolving
     *  true on readiness / false when `timeoutMs` expires. Input written into a booting agent
     *  is swallowed or mis-submitted, so the content send holds until this yields true.
     *  Optional — absent (older callers/tests) ⇒ always ready. */
    waitInputReady?: (timeoutMs: number) => true | Promise<boolean>;
    /** Whether the text field starts expanded (mobile) or collapsed to the bar (desktop). */
    defaultOpen?: boolean;
    /** Engine-qualified session key (`<engine>:<id>`) this box composes for, used to
     *  persist the draft server-side (#477). `null`/absent ⇒ drafts disabled (a not-yet-real
     *  `new-…` placeholder session has no metadata key — out of scope). */
    sessionId?: string | null;
  }
>(function Compose(
  {
    sendInput,
    connEpoch,
    waitInputReady,
    defaultOpen = true,
    sessionId = null,
  },
  ref,
) {
  const [open, setOpen] = useState(defaultOpen);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // #533: token guarding the fresh-launch readiness hold — a newer Send supersedes a pending one.
  const holdRef = useRef(0);
  // #619: recoverable history of sent messages. Read lazily (localStorage) and refreshed on every
  // send / modal open, so a send from another tab shows up without a reload.
  const [history, setHistory] = useState<SentMessage[]>(() => readSent());
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyBtnRef = useRef<HTMLElement | null>(null);

  // Push-to-talk dictation (#483, made a REAL hold in #738). At most one active recognizer
  // (`recogRef`); `dictBaseRef` is the draft text present when dictation began. Each result event
  // rebuilds the transcript from the engine's cumulative results list (#487 — no per-event
  // accumulation). `listening` drives the chip while the control is held; `finalizing` covers the
  // window after release where the mic is off but the engine still owes us the tail (#738).
  const [listening, setListening] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const recogRef = useRef<SpeechRecognition | null>(null);
  const dictBaseRef = useRef("");
  // #711: per-entry evidence for the snapshot collapse — what each results-list entry last said,
  // when (and in which onresult event) it last changed, and whether it was already final when it
  // first appeared. Indexed like e.results; reset per recognizer (the service-not-allowed fallback
  // builds a fresh engine whose entries must earn their own history). See lib/dictation.ts for how
  // the evidence is weighed.
  const entryTextRef = useRef<string[]>([]);
  const entryAtRef = useRef<number[]>([]);
  const entryFirstAtRef = useRef<number[]>([]); // arrival, not latest revision (#749)
  const entryEventRef = useRef<number[]>([]);
  const entryFinalBornRef = useRef<boolean[]>([]);
  const dictEventSeqRef = useRef(0);
  // Guards the async mic-permission grant AND the deferred re-arm: bumped on every start AND stop,
  // so a getUserMedia promise (or a queued re-arm) that lands after the user already cancelled or
  // restarted doesn't spin up a stale recognizer.
  const dictTokenRef = useRef(0);
  // #736: dictation spans MANY engine sessions. `dictWanted` is the user's intent — true from the
  // press until release / a fatal error / a bound firing — and is what an ended session consults
  // before re-arming. `dictText` is the full textarea text as dictation last wrote it: the anchor a
  // re-armed recognizer picks up from, so utterance 2 appends to utterance 1 instead of re-anchoring
  // on the pre-dictation draft captured in the previous session's closure. `dictLastSpeechAt` /
  // `dictDeadStarts` carry the two re-arm bounds across sessions (see DICTATION_IDLE_STOP_MS).
  const dictWantedRef = useRef(false);
  const dictTextRef = useRef("");
  const dictLastSpeechRef = useRef(0);
  // #738 hold bookkeeping. `heldPointer` is the pointerId that OWNS the gesture — a second contact's
  // events are ignored, so a stray thumb can neither restart nor release an active hold. `keyHold`
  // is the same ownership for a key-initiated hold (the keyup is listened for on `window`, since the
  // release lands wherever focus went by then). `finalizingRef` mirrors the `finalizing` state for
  // the event handlers, and `finalizeTimer` bounds the wait for an `onend` that may never come.
  const heldPointerRef = useRef<number | null>(null);
  const keyHoldRef = useRef<string | null>(null);
  const finalizingRef = useRef(false);
  const finalizeTimerRef = useRef<number | undefined>(undefined);
  const micBtnRef = useRef<HTMLButtonElement | null>(null);
  const dictDeadStartsRef = useRef(0);
  // The idle deadline is a real TIMER, not a check on the way out of a session (Hermes on #736):
  // a recognizer can stay open and silent forever — the engine is under no obligation to hang up —
  // and then no callback ever runs to notice. Only a timer bounds the mic hold in that shape.
  const dictIdleTimerRef = useRef<number | undefined>(undefined);

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
    saveTimerRef.current = window.setTimeout(
      () => flushDraft(text, attachments),
      DRAFT_SAVE_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(saveTimerRef.current);
  }, [text, attachments, sessionId, flushDraft]);

  // Flush a pending draft on unmount (Terminal remounts on session switch) so the last edits
  // aren't lost. `[]` deps → cleanup runs only on real unmount, reading the latest via refs.
  useEffect(() => {
    return () => {
      if (dirtyRef.current && sidRef.current) {
        const { text: t, attachments: a } = latestRef.current;
        if (draftSignature(t, a) !== lastSavedRef.current && sidRef.current) {
          void api
            .saveDraft(sidRef.current, { text: t, attachments: a })
            .catch(() => {});
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

  const clearIdleStop = () => window.clearTimeout(dictIdleTimerRef.current);

  // DISCARD the active recognizer — unmount, session switch, or a bound that ends dictation outright.
  // Marks it superseded BEFORE stopping and drops its handlers, so any late callback from this
  // instance is ignored (#483). Nothing is waiting on the result, so `stop()`'s trailing delivery is
  // deliberately thrown away; `releaseDictation` is the path that keeps it (#738).
  const stopDictation = useCallback(() => {
    const r = recogRef.current;
    recogRef.current = null;
    window.clearTimeout(dictIdleTimerRef.current); // the deadline dies with the dictation
    window.clearTimeout(finalizeTimerRef.current);
    dictWantedRef.current = false; // the user is done: an in-flight session end must not re-arm
    finalizingRef.current = false;
    heldPointerRef.current = null;
    keyHoldRef.current = null;
    dictTokenRef.current++; // invalidate any in-flight getUserMedia grant / queued re-arm
    setListening(false);
    setFinalizing(false);
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

  // RELEASE — the user let go (#738). This is NOT the discard path: `stop()` ends capture but the
  // engine still delivers what it already heard, so `onresult` stays attached and `recogRef` stays
  // set, and that trailing final lands in the textarea like any other. What ends is the user's
  // intent, cleared BEFORE `stop()` so the `onend` that follows takes finishSession's "done" branch
  // instead of re-arming (#736). Teardown completes in that `onend`; `finalizeTimer` is the backstop
  // for an engine that never sends one, because a wedged engine must not strand the chip.
  const releaseDictation = useCallback(() => {
    heldPointerRef.current = null;
    keyHoldRef.current = null;
    if (!dictWantedRef.current && !recogRef.current) return; // nothing held
    window.clearTimeout(dictIdleTimerRef.current);
    dictWantedRef.current = false;
    dictTokenRef.current++; // a grant still in flight must not arm a recognizer after the release
    setListening(false);
    const r = recogRef.current;
    if (!r) {
      // The hold ended before a recognizer existed (a tap, or a release during the mic grant):
      // nothing is finalizing, so there is no window to enter.
      finalizingRef.current = false;
      setFinalizing(false);
      return;
    }
    finalizingRef.current = true;
    setFinalizing(true);
    window.clearTimeout(finalizeTimerRef.current);
    finalizeTimerRef.current = window.setTimeout(() => {
      if (finalizingRef.current) stopDictation();
    }, DICTATION_FINALIZE_MS);
    try {
      r.stop(); // end capture, keep the tail
    } catch {
      /* already stopped — the pending onend still completes the teardown */
    }
  }, [stopDictation]);

  // (Re)start the idle deadline: from the press, and again on every word actually heard. It fires
  // only if DICTATION_IDLE_STOP_MS passes with no speech at all — whatever the engine is doing,
  // open session or re-arm chain — and stops the dictation for real, releasing the mic.
  const armIdleStop = () => {
    clearIdleStop();
    dictIdleTimerRef.current = window.setTimeout(() => {
      if (dictWantedRef.current) stopDictation();
    }, DICTATION_IDLE_STOP_MS);
  };

  // What happens when an engine SESSION ends (#736) — the shared tail of `onend` and of a `start()`
  // that threw. While the user still wants to dictate and both bounds hold, a fresh recognizer is
  // armed and dictation simply continues; otherwise the chip clears for real. The re-arm is deferred
  // a tick because Chrome can still be tearing the previous session down inside `onend`, where a
  // synchronous `start()` throws InvalidStateError — and `dictTokenRef` is re-checked when the tick
  // runs, so a stop during that window wins over the queued re-arm.
  const finishSession = (
    SR: SpeechRecognitionStatic,
    continuousMode: boolean,
    startedAt: number,
    heardSpeech: boolean,
  ) => {
    if (!dictWantedRef.current) {
      // The user let go. This `onend` is the engine confirming it has delivered everything it had,
      // so the release completes here: drop the recognizer, close the finalizing window, and let the
      // next hold through (#738).
      recogRef.current = null;
      window.clearTimeout(finalizeTimerRef.current);
      finalizingRef.current = false;
      setFinalizing(false);
      setListening(false);
      return;
    }
    const now = performance.now();
    // A session that heard speech proves the engine works, whatever it did afterwards.
    if (heardSpeech) dictDeadStartsRef.current = 0;
    else if (now - startedAt < DICTATION_DEAD_START_MS)
      dictDeadStartsRef.current++;
    const engineDead = dictDeadStartsRef.current >= DICTATION_DEAD_START_LIMIT;
    // The deadline is enforced by `armIdleStop`'s timer; re-checking it here only means a session
    // that ends past it isn't re-armed for the moments before that timer gets its turn.
    const goneQuiet = now - dictLastSpeechRef.current > DICTATION_IDLE_STOP_MS;
    if (engineDead || goneQuiet) {
      dictWantedRef.current = false;
      clearIdleStop();
      setListening(false);
      // Silence is a normal way to stop (the user walked away); an engine that won't run is not —
      // say so rather than letting the chip wink out unexplained, which is how #736 was reported.
      if (engineDead) {
        setNote(
          "dictation stopped — the speech engine kept dropping the session",
        );
        setTimeout(() => setNote(""), 4000);
      }
      return;
    }
    const token = dictTokenRef.current;
    window.setTimeout(() => {
      if (!dictWantedRef.current || dictTokenRef.current !== token) return; // stopped meanwhile
      beginRecognition(SR, continuousMode);
    }, 0);
  };

  // Spin up a fresh recognizer, anchor the current draft, and stream interim + final results into
  // the textarea via the SAME setText + grow + dirty path as typing (#483/#477). `continuousMode`
  // is false on the Android-Chrome retry path (see startDictation): Android Chrome rejects a
  // continuous recognizer with `service-not-allowed`, so we fall back to single-utterance mode.
  const beginRecognition = (
    SR: SpeechRecognitionStatic,
    continuousMode: boolean,
  ) => {
    const r = new SR();
    r.continuous = continuousMode;
    r.interimResults = true;
    const lang =
      (typeof navigator !== "undefined" && navigator.language) || "en-US";
    r.lang = lang;
    // Selects the transcript-comparison mode for the snapshot collapse (#711 finding 3): whole
    // words where spaces delimit them, codepoint prefixes where they don't (CJK etc.).
    const spaceDelimited = isSpaceDelimitedLang(lang);
    // Anchor on what dictation has typed so far, not on the `text` of the render that built this
    // callback: on a re-arm (#736) that closure is a session stale, and using it would drop every
    // utterance before this one. `startDictation` seeds the ref with the live draft.
    dictBaseRef.current = dictTextRef.current;
    // Per-recognizer evidence for the #711 collapse: a fresh engine's entries earn their own
    // history, so the collapse never reasons across a session boundary.
    entryTextRef.current = [];
    entryAtRef.current = [];
    entryFirstAtRef.current = [];
    entryEventRef.current = [];
    entryFinalBornRef.current = [];
    const startedAt = performance.now();
    let heardSpeech = false; // this session, for the dead-start bound in finishSession
    r.onresult = (e) => {
      if (recogRef.current !== r) return; // superseded recognizer — ignore late results
      // Rebuild the transcript from scratch on every event — never accumulate across events, so
      // Chrome re-firing onresult for the SAME finalized utterance is idempotent rather than
      // typing the phrase 10× (#487). Two layers then clean up the engine's own duplication:
      //   • Only the LAST entry can be a live interim. An earlier non-final entry is a stale
      //     snapshot the engine stacked instead of replacing, so it's dropped (#649) — this also
      //     covers an engine that revises a phrase between interim snapshots.
      //   • assembleSpoken drops a finalized entry that merely restates its neighbour — but ONLY
      //     on an engine that positively identified itself as snapshot-stacking by finalizing an
      //     empty entry (#711 finding 3, a spec violation no compliant engine produces), and then
      //     only with per-pair evidence (birth-final + later-event + burst arrival + the text
      //     itself). Finals from any other engine are concatenated verbatim, whatever their
      //     shape or timing.
      const now = performance.now();
      const eventSeq = ++dictEventSeqRef.current;
      const segs: SpokenSegment[] = [];
      for (let i = 0; i < e.results.length; i++) {
        const res = e.results[i];
        const transcript = res[0].transcript;
        if (entryTextRef.current[i] === undefined) {
          entryFinalBornRef.current[i] = !!res.isFinal;
          entryFirstAtRef.current[i] = now; // when it ARRIVED — never revised afterwards (#749)
        }
        if (entryTextRef.current[i] !== transcript) {
          entryTextRef.current[i] = transcript;
          entryAtRef.current[i] = now;
          entryEventRef.current[i] = eventSeq;
        }
        if (res.isFinal || i === e.results.length - 1) {
          segs.push({
            text: transcript,
            atMs: entryAtRef.current[i],
            firstAtMs: entryFirstAtRef.current[i],
            eventSeq: entryEventRef.current[i],
            isFinal: !!res.isFinal,
            finalBorn: entryFinalBornRef.current[i],
          });
        }
      }
      const spoken = assembleSpoken(segs, spaceDelimited);
      if (spoken) {
        heardSpeech = true;
        dictLastSpeechRef.current = now;
        armIdleStop(); // speech pushes the deadline out (#736)
      }
      dirtyRef.current = true; // dictation is draftable content, just like typing
      const next = joinSpoken(dictBaseRef.current, spoken);
      dictTextRef.current = next; // the anchor a re-armed session picks up from
      setText(next);
      grow();
    };
    r.onerror = (e) => {
      if (recogRef.current !== r) return;
      // Android Chrome rejects a continuous recognizer with `service-not-allowed`; retry ONCE with
      // a single-utterance recognizer before surfacing the error. `continuousMode` gates the retry
      // so the fallback can't loop.
      if (e.error === "service-not-allowed" && continuousMode) {
        recogRef.current = null;
        beginRecognition(SR, false);
        return;
      }
      // `no-speech` is the endpointer giving up on a silent stretch, not a failure — the session
      // ends either way, so leave the recognizer in place and let `onend` re-arm it (#736). That
      // is what makes a pause between sentences survivable instead of terminal.
      if (e.error === "no-speech") return;
      recogRef.current = null;
      dictWantedRef.current = false; // a real failure ends the dictation, not just the session
      clearIdleStop();
      setListening(false);
      if (e.error && e.error !== "aborted") {
        setNote(micErrorNote(e.error));
        setTimeout(() => setNote(""), 4000);
      }
    };
    r.onend = () => {
      if (recogRef.current !== r) return; // a fresh recognizer already took over
      recogRef.current = null;
      finishSession(SR, continuousMode, startedAt, heardSpeech);
    };
    recogRef.current = r;
    setListening(true);
    try {
      r.start();
    } catch {
      // start() throws if the engine is already running or still tearing a session down. Treat it
      // exactly like a session that ended without hearing anything: the bounded re-arm path retries
      // and gives up after DICTATION_DEAD_START_LIMIT of them, rather than spinning (#736).
      recogRef.current = null;
      finishSession(SR, continuousMode, startedAt, false);
    }
  };

  // Hold start (#483, #738): FIRST acquire the mic explicitly via getUserMedia, THEN build the
  // recognizer. Android Chrome's SpeechRecognition does not reliably obtain the mic on its own —
  // start() fails with `not-allowed` even when the OS + site permission are granted — so we trigger
  // the real grant with getUserMedia (which resolves silently when already allowed, or prompts once)
  // and release the stream immediately, since the recognizer captures on its own. A rejected grant
  // names the actual reason via micErrorNote instead of a generic "blocked".
  const startDictation = () => {
    const SR = getSpeechRecognition();
    if (!SR) return;
    // A hold arriving inside the finalizing window is IGNORED, not queued (#738). Starting here
    // would take the discard path below on a recognizer that still owes us its last result — which
    // would drop exactly the trailing phrase the release is waiting for.
    if (finalizingRef.current) return;
    if (recogRef.current) stopDictation();
    const token = ++dictTokenRef.current;
    // Fresh dictation intent (#736): the mic stays armed across engine session ends until the user
    // lets go or a bound fires. Seed the anchor with the live draft and the idle clock with now,
    // so the first silent stretch is measured from the press rather than a previous dictation.
    dictWantedRef.current = true;
    dictTextRef.current = text;
    dictLastSpeechRef.current = performance.now();
    dictDeadStartsRef.current = 0;
    armIdleStop(); // the deadline runs from the press, even if not a word is ever heard
    setListening(true); // optimistic chip; cleared below if the grant/start fails
    const md =
      typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
    if (!md?.getUserMedia) {
      // Old / insecure context without mediaDevices — let the recognizer request the mic itself.
      beginRecognition(SR, true);
      return;
    }
    md.getUserMedia({ audio: true })
      .then((stream) => {
        stream.getTracks().forEach((t) => t.stop()); // release; the recognizer captures its own
        if (dictTokenRef.current !== token) return; // cancelled / restarted during the async grant
        beginRecognition(SR, true);
      })
      .catch((err: unknown) => {
        if (dictTokenRef.current !== token) return;
        dictWantedRef.current = false; // no mic, no dictation to re-arm
        clearIdleStop();
        setListening(false);
        setNote(micErrorNote(gumErrorCode(err)));
        setTimeout(() => setNote(""), 4000);
      });
  };

  // --- The hold gesture (#738) ------------------------------------------------------------------
  // Pointer down starts, pointer up / cancel releases. `setPointerCapture` is what makes a release
  // reliable: without it a finger that slides off this 34px-tall chip mid-sentence never delivers its
  // `pointerup` and the mic sticks on. It is also why we deliberately do NOT cancel on `pointerleave`
  // the way the connect-page hold gate does (`homefree/connect.main.ts`, #690) — leaving the button
  // is not letting go, and treating it as such would truncate speech, the very complaint behind #736.
  const onMicPointerDown = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (e.pointerType === "mouse" && e.button !== 0) return; // left button only
    if (heldPointerRef.current !== null) return; // a second contact never steals an active hold
    if (keyHoldRef.current) return; // …nor does a pointer steal a key-owned hold (Hermes on #738)
    if (finalizingRef.current) return; // the previous release is still finishing (#738)
    e.preventDefault(); // no text-selection drag, no synthesised mouse events after a touch
    heldPointerRef.current = e.pointerId;
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* capture unsupported — the blur / visibilitychange backstops still end the hold */
    }
    startDictation();
  };
  const onMicPointerUp = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (heldPointerRef.current !== e.pointerId) return; // not the pointer that owns this hold
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* never captured */
    }
    releaseDictation();
  };

  // Whether a keystroke landing on this element is already spoken for, so the global Space hold must
  // keep its hands off (#738). Editables cover the compose box AND the terminal — xterm focuses a
  // hidden textarea, so a Space meant for the PTY is caught by the same check. Activatable controls
  // own Space as their native activation key.
  const claimsSpaceKey = (el: Element | null): boolean => {
    if (!(el instanceof HTMLElement)) return false;
    if (el.isContentEditable) return true;
    if (["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(el.tagName))
      return true;
    return el.getAttribute("role") === "button";
  };

  // Global key hold. Two gestures share it: Space/Enter while the chip itself is focused, and — the
  // desktop convenience — Space anywhere that has no claim on the key. The keyup is bound to `window`
  // rather than the button because focus can move mid-hold, and a button-scoped listener would then
  // miss the release and leave the mic open (the lesson already encoded in #690's gate).
  // No dependency array on purpose: the handlers must see the CURRENT start/release closures, and
  // re-binding four listeners per render is cheaper than the stale-closure bugs a deps list invites.
  useEffect(() => {
    if (!getSpeechRecognition()) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== " " && e.key !== "Enter") return;
      if (e.repeat || keyHoldRef.current) return; // OS auto-repeat is not a new press
      // Ownership spans BOTH input kinds, not just pointers (Hermes on #738). A key press during a
      // pointer-owned hold used to reach startDictation(), which — seeing a live recognizer — took
      // the discard path and killed the held session along with the phrase it was finalizing. The
      // first input to take the hold keeps it until it lets go.
      if (heldPointerRef.current !== null) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return; // leave shortcuts alone
      const onChip = e.target === micBtnRef.current;
      if (!onChip) {
        if (e.key !== " ") return; // only Space is the global hotkey; Enter stays local to the chip
        if (claimsSpaceKey(e.target as Element | null)) return;
        if (document.querySelector('[role="dialog"], dialog[open]')) return; // a modal owns its keys
      }
      if (finalizingRef.current) return;
      e.preventDefault(); // no page scroll, no implicit button click on keyup
      keyHoldRef.current = e.key; // remember WHICH key owns it, so another key can't release it
      setOpen(true); // never dictate into a box the user cannot see
      startDictation();
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (!keyHoldRef.current || e.key !== keyHoldRef.current) return; // not the key holding this
      releaseDictation();
    };
    const onLeave = () => {
      // Focus or visibility leaving mid-hold is a release: the keyup may never arrive.
      if (keyHoldRef.current || heldPointerRef.current !== null)
        releaseDictation();
    };
    const onVisibility = () => {
      if (document.hidden) onLeave();
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onLeave);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onLeave);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  });

  // Stop dictation when the box collapses (the mic chip only lives in the open state) and abort it
  // on unmount / session switch, so a recognizer never outlives the compose box it dictates into.
  useEffect(() => {
    if (!open) stopDictation();
  }, [open, stopDictation]);
  useEffect(() => {
    return () => {
      const r = recogRef.current;
      recogRef.current = null;
      dictWantedRef.current = false; // no re-arm can outlive the box (#736)
      window.clearTimeout(dictIdleTimerRef.current); // nor the idle deadline
      dictTokenRef.current++; // invalidate a still-pending getUserMedia grant / queued re-arm so it
      // can't build a recognizer after the box has unmounted (the async twin of stopDictation's guard).
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

  // #619: drop a recorded message back into the composer. It takes the SAME path as typing and the
  // attachment pills — mark dirty, then flush the draft immediately — or a restore made just before
  // a refresh / session switch would be lost again before the autosave debounce ever ran.
  const restoreSent = (entry: SentMessage) => {
    const atts: Attachment[] = entry.attachments.map((path) => ({
      name: path.split("/").pop() || path,
      path,
    }));
    setText(entry.text);
    setAttachments(atts);
    dirtyRef.current = true;
    flushDraft(entry.text, atts);
    setHistoryOpen(false);
    setOpen(true); // the composer may be collapsed (desktop default) — surface what we restored
    requestAnimationFrame(() => {
      taRef.current?.focus();
      grow();
    });
  };

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
    // #619: record the submission BEFORE anything is delivered or cleared. Whatever happens next —
    // a dropped frame, or an agent that silently swallows the paste (#616) — the text is recoverable
    // from the history modal. Recorded unconfirmed; the deferred Enter below flips it. Fail-soft:
    // a null id (no localStorage / quota) just means no safety net, never a blocked send.
    const historyId = appendSent({
      text: savedText,
      attachments: savedAttachments.map((a) => a.path),
      session: sessionId,
    });
    setHistory(readSent());

    // A (re)paste that didn't reach the socket means the message isn't there — restore the composer
    // and surface why, and (the caller) must NOT submit a bare Enter (that's the empty-turn bug).
    const abortNotDelivered = (why = "reconnecting — not sent, try again") => {
      setText(savedText);
      setAttachments(savedAttachments);
      // #477: the turn wasn't submitted — guarantee the restored content is persisted (it may not
      // have been debounce-saved yet, and a later clear must not win), so a reload / session switch
      // keeps the draft. flushDraft is a no-op when the server already holds this exact content.
      dirtyRef.current = true;
      flushDraft(savedText, savedAttachments);
      setNote(why);
      setTimeout(() => setNote(""), 3000);
    };
    const deliver = () => {
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
      const enterDelay =
        savedAttachments.length > 0
          ? ENTER_DELAY_AFTER_ATTACHMENT_MS
          : ENTER_DELAY_MS;
      // Clear the prompt line, then bracketed-paste the message. If the socket is mid-reconnect the
      // paste WON'T deliver (`sendInput` returns false) — do NOT fire a bare Enter later, or it
      // submits an EMPTY turn (#287). Keep the text so the user can resend, and say why.
      sendInput(KEYSEQ.ctrla + KEYSEQ.ctrlk);
      if (!sendInput(bracketedPaste(msg))) {
        abortNotDelivered(); // socket mid-reconnect → not sent; never fire a bare Enter
        return;
      }
      // The Enter is deferred so the agent reads it as a discrete keystroke AFTER the paste-end
      // marker (#180/#226). But a reconnect can land in that gap: the paste went to the now-dead
      // socket while the Enter would hit a FRESH socket that never received it → empty turn. Gate on
      // the socket id: if it changed, re-send clear+paste on the new socket first (the clear
      // prevents any doubling) — and if THAT re-paste also fails (a second reconnect), abort
      // instead of submitting empty.
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
        // Delivered: every frame reached the socket. Flip the history entry out of UNCONFIRMED —
        // which asserts delivery to the SOCKET, never that the agent processed the turn (#619).
        if (historyId) {
          confirmSent(historyId);
          setHistory(readSent());
        }
        // Clear the composer AND the server draft (a just-sent turn must not linger as a
        // draft). clearDraft cancels any pending debounce so a trailing flush can't resurrect it.
        setText("");
        setAttachments([]);
        clearDraft();
        if (taRef.current) taRef.current.style.height = "auto";
      }, enterDelay);
    };
    // Fresh-launch readiness hold (#533): input written into a still-booting agent is swallowed
    // (the composed text) or mis-submitted (the incident's first turn was the literal Ctrl-A of
    // the clear). `true` — the common case, including every attach to a running session — keeps
    // the delivery fully synchronous so the established frame sequencing is untouched. Otherwise
    // hold visibly, deliver on readiness, and give up (draft intact) when the bounded wait fails.
    const ready = waitInputReady?.(READY_WAIT_MS) ?? true;
    if (ready === true) {
      deliver();
      return;
    }
    const token = ++holdRef.current;
    setNote("waiting for agent…");
    void ready.then((ok) => {
      if (holdRef.current !== token) return; // superseded by a newer Send
      if (!ok) {
        abortNotDelivered("agent not ready — not sent, try again");
        return;
      }
      setNote("");
      deliver();
    });
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

  const pickFiles = (files: FileList | null) =>
    uploadFiles(Array.from(files ?? []));

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
  // to the textarea (#135). When the DataTransfer yields no usable file AND no text —
  // deferred clipboard backends (observed: Windows Chrome 149) can deliver exactly that
  // for a real image paste — fall back to reading the async clipboard inside the same
  // gesture instead of silently doing nothing (#530).
  const onPaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const images = imageFilesFromData(e.clipboardData);
    if (images.length) {
      e.preventDefault();
      void uploadFiles(images);
      return;
    }
    if (e.clipboardData?.getData("text/plain")) return; // normal text paste — textarea handles it
    const hadFileKind = Array.from(e.clipboardData?.items ?? []).some(
      (i) => i.kind === "file",
    );
    e.preventDefault(); // nothing would have pasted anyway
    void imageFilesFromAsyncClipboard().then((fallback) => {
      if (fallback.length) {
        void uploadFiles(fallback);
      } else if (hadFileKind) {
        // The event claimed to carry a file we couldn't read anywhere — say so instead of
        // leaving the paste a dead keystroke.
        setNote("couldn't read image from clipboard");
        setTimeout(() => setNote(""), 3000);
      }
    });
  };

  // Hide the mic entirely where the browser has no speech engine (e.g. Firefox) — a natural empty
  // state, no feature flag (#483). Re-read each render so a test stub installed on `window` is seen.
  const speechSupported = !!getSpeechRecognition();

  // The single collapsible group (#487), in order: up, down, return, esc, tab, attach, and — when
  // open — collapse. Everything else (mic, Send) sits inline to the right; no second menu.
  const keyActions: KeyAction[] = [
    {
      id: "up",
      aria: "Up",
      title: "Up",
      icon: <ArrowUp size={16} />,
      run: () => sendInput(KEYSEQ.up),
    },
    {
      id: "down",
      aria: "Down",
      title: "Down",
      icon: <ArrowDown size={16} />,
      run: () => sendInput(KEYSEQ.down),
    },
    {
      id: "enter",
      aria: "Return",
      title: "Return",
      icon: <CornerDownLeft size={16} />,
      run: () => sendInput(KEYSEQ.enter),
    },
    {
      id: "esc",
      aria: "Escape",
      title: "Escape",
      text: "esc",
      run: () => sendInput(KEYSEQ.esc),
    },
    {
      id: "tab",
      aria: "Tab",
      title: "Tab",
      icon: <ArrowRightToLine size={16} />,
      run: () => sendInput(KEYSEQ.tab),
    },
    {
      id: "attach",
      aria: "Attach file",
      title: "Attach an image or file",
      icon: <Paperclip size={16} />,
      run: () => fileRef.current?.click(),
    },
    ...(history.length > 0
      ? [
          {
            id: "history",
            aria: "Sent messages",
            title: `Sent messages (last ${history.length})`,
            icon: <History size={16} />,
            run: () => {
              // The chip may be inline or inside KeyBar's "…" overflow menu — either way the
              // trigger is whatever holds focus, and focus returns there on close.
              historyBtnRef.current =
                document.activeElement as HTMLElement | null;
              setHistory(readSent()); // another tab may have sent since we last looked
              setHistoryOpen(true);
            },
          },
        ]
      : []),
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
            ref={micBtnRef}
            className={
              listening
                ? `${styles.mic} ${styles.micOn}`
                : finalizing
                  ? `${styles.mic} ${styles.micFinalizing}`
                  : styles.mic
            }
            // The accessible name still leads with "…voice input" so it reads as the same control it
            // has always been, with the gesture spelled out after (#738).
            aria-label={
              listening
                ? "Stop voice input — release to finish"
                : finalizing
                  ? "Finishing voice input"
                  : "Start voice input — hold to talk"
            }
            aria-pressed={listening}
            aria-disabled={finalizing}
            title={
              listening
                ? "Release to stop — the last phrase still lands"
                : finalizing
                  ? "Finishing transcription…"
                  : "Hold to talk (or hold Space outside a text field)"
            }
            onPointerDown={onMicPointerDown}
            onPointerUp={onMicPointerUp}
            onPointerCancel={onMicPointerUp}
            onContextMenu={(e) => e.preventDefault()} // a long press is a hold, not a menu
          >
            <Mic size={16} />
            <span className={styles.micLabel}>Push to talk</span>
          </button>
        )}
        {open ? (
          <button
            type="button"
            className={`${styles.send} shine`}
            title="Send + Enter"
            onClick={send}
          >
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

      {historyOpen && (
        <SentMessagesModal
          entries={history}
          currentSession={sessionId}
          onRestore={restoreSent}
          onClose={() => setHistoryOpen(false)}
          returnFocusTo={historyBtnRef.current}
        />
      )}
    </div>
  );
});
