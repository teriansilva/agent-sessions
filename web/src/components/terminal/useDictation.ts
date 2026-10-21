import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { assembleSpoken, isSpaceDelimitedLang, type SpokenSegment } from "../../lib/dictation";

/** Push-to-talk dictation as a hook (#1332 Phase 3c), moved out of `Compose` so the terminal
 *  composer and the API session composer share ONE recognizer lifecycle — every hardening below
 *  (#483 #487 #649 #711 #736 #738 #749 #792 #908) applies to both. The host owns the draft: the
 *  hook reads it at a press and writes the dictated text through `writeDraft`, which is the
 *  host's own typing path. */

/** Monotonic milliseconds — only ever called from engine/timer callbacks, never during render. */
const nowMs = (): number => performance.now();

export type DictationOptions = {
  /** The live draft text, read when a hold starts (dictation anchors on it). */
  readDraft: () => string;
  /** Write the full dictated text — the host's typing path (set, grow, mark dirty). */
  writeDraft: (text: string) => void;
  /** A transient note for the operator (the host clears it after a few seconds). */
  note: (message: string) => void;
  /** Runs where a recognizer (re)anchors and where a release completes: Compose lands the path
   *  tokens it queued behind a dictation handoff here (#792). */
  onAnchor?: () => void;
  /** A global key hold is starting: make the box visible (Compose opens itself). */
  onKeyHold?: () => void;
  /** Hold Space anywhere that does not claim the key to dictate (the terminal composer's desktop
   *  convenience). A second composer on the page leaves this off so it never takes Space. */
  globalSpace: boolean;
};

/** Push-to-talk dictation (#483, a real press-and-hold since #738): the browser's own speech
 *  engine, vendor-prefixed on Chromium.
 *  Read lazily (not a module constant) so tests can install a stub on `window` before render and
 *  so an unsupported browser (e.g. Firefox) simply yields `undefined` → the mic chip isn't shown. */
export const getSpeechRecognition = (): SpeechRecognitionStatic | undefined =>
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

export function useDictation(opts: DictationOptions, active: boolean) {
  // The host's callbacks change every render; handlers read the latest through this ref.
  const optsRef = useRef(opts);
  useLayoutEffect(() => {
    optsRef.current = opts;
  });

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
  /** Whether the LIVE recognizer was armed continuous. `beginRecognition` takes it as a
   *  parameter, so a re-arm from outside that call (the #792 insert) needs it recorded. */
  const dictContinuousRef = useRef(true);
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
      // so the release completes here: drop the recognizer, land any path tokens that were queued
      // behind this stop() (a release must not leave them to the fallback timer — #908 round 8),
      // close the finalizing window, and let the next hold through (#738).
      recogRef.current = null;
      optsRef.current.onAnchor?.();
      window.clearTimeout(finalizeTimerRef.current);
      finalizingRef.current = false;
      setFinalizing(false);
      setListening(false);
      return;
    }
    const now = nowMs();
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
        optsRef.current.note("dictation stopped — the speech engine kept dropping the session",);
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
    dictContinuousRef.current = continuousMode;
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
    // A token queued by an insert lands HERE, at the handoff: the superseded session has
    // delivered its tail into `dictTextRef` by now, so the path is appended after the words that
    // were actually spoken, and the fresh session anchors on the result.
    optsRef.current.onAnchor?.();
    dictBaseRef.current = dictTextRef.current;
    // Per-recognizer evidence for the #711 collapse: a fresh engine's entries earn their own
    // history, so the collapse never reasons across a session boundary.
    entryTextRef.current = [];
    entryAtRef.current = [];
    entryFirstAtRef.current = [];
    entryEventRef.current = [];
    entryFinalBornRef.current = [];
    const startedAt = nowMs();
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
      const now = nowMs();
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
      const next = joinSpoken(dictBaseRef.current, spoken);
      dictTextRef.current = next; // the anchor a re-armed session picks up from
      optsRef.current.writeDraft(next); // the host's typing path: set, grow, mark dirty
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
        optsRef.current.note(micErrorNote(e.error));
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
    dictTextRef.current = optsRef.current.readDraft();
    dictLastSpeechRef.current = nowMs();
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
        optsRef.current.note(micErrorNote(gumErrorCode(err)));
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
        if (!optsRef.current.globalSpace) return; // a host that leaves Space alone (the API pane)
        if (e.key !== " ") return; // only Space is the global hotkey; Enter stays local to the chip
        if (claimsSpaceKey(e.target as Element | null)) return;
        if (document.querySelector('[role="dialog"], dialog[open]')) return; // a modal owns its keys
      }
      if (finalizingRef.current) return;
      e.preventDefault(); // no page scroll, no implicit button click on keyup
      keyHoldRef.current = e.key; // remember WHICH key owns it, so another key can't release it
      optsRef.current.onKeyHold?.(); // never dictate into a box the user cannot see
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

  // Stop dictation when the host deactivates it (Compose: the box collapsed) and abort it
  // on unmount / session switch, so a recognizer never outlives the compose box it dictates into.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- tearing down a recognizer is the effect
    if (!active) stopDictation();
  }, [active, stopDictation]);
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

  return {
    supported: !!getSpeechRecognition(),
    listening,
    finalizing,
    micBtnRef,
    micHandlers: {
      onPointerDown: onMicPointerDown,
      onPointerUp: onMicPointerUp,
      onPointerCancel: onMicPointerUp,
      onContextMenu: (e: { preventDefault: () => void }) => e.preventDefault(), // a hold, not a menu
    },
    stop: stopDictation,
    release: releaseDictation,
    /** No hold, no recognizer, no finalizing window: nothing still owes the draft words. */
    settled: () => !dictWantedRef.current && !recogRef.current && !finalizingRef.current,
    finalizingNow: () => finalizingRef.current,
    /** The full text as dictation last wrote it (a host edit keeps it in step, #792). */
    textRef: dictTextRef,
    hasRecognizer: () => recogRef.current !== null,
    /** End capture but keep the tail (#792's lossless handoff). False when there is nothing to
     *  stop or the engine refused — the host then applies its queued work at once. */
    stopCapture: (): boolean => {
      const r = recogRef.current;
      if (!r) return false;
      try {
        r.stop();
        return true;
      } catch {
        return false;
      }
    },
    /** Drop the live recognizer WITHOUT its tail and re-arm if the hold continues — the bounded
     *  fallback for an engine that never ends (#792's forcePendingHandoff). */
    supersede: () => {
      optsRef.current.onAnchor?.();
      const r = recogRef.current;
      if (!r) return;
      const SR = getSpeechRecognition();
      recogRef.current = null; // supersede FIRST: every handler ignores a stale recognizer
      r.onresult = null;
      r.onerror = null;
      r.onend = null;
      try {
        r.abort(); // discard whatever it still owes us; `stop()` would ask for it instead
      } catch {
        /* already gone */
      }
      if (SR && dictWantedRef.current) beginRecognition(SR, dictContinuousRef.current);
    },
  };
}
