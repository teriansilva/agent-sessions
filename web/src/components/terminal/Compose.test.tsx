import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { bracketedPaste, KEYSEQ } from "../../lib/termKeys";
import { appendSent, readSent } from "../../lib/sentHistory";
import { Compose, type ComposeHandle } from "./Compose";

vi.mock("../../lib/api", () => ({
  api: {
    upload: vi.fn(),
    // #477: Compose loads/saves its draft when given a sessionId. Default to an empty draft so the
    // non-draft tests (rendered without a sessionId) are unaffected; the draft test asserts on these.
    getDraft: vi.fn(() => Promise.resolve({ id: "", text: "", attachments: [], updated_at: null })),
    saveDraft: vi.fn(() => Promise.resolve({ id: "", has_draft: false })),
  },
}));

let sendInput: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear(); // #619: the sent-message ring is device-global
  sendInput = vi.fn(() => true); // default: every frame is delivered (socket OPEN)
});

function renderCompose(connEpoch: () => number = () => 1) {
  return render(<Compose sendInput={sendInput} connEpoch={connEpoch} />);
}

// --- Push-to-talk dictation (#483) -------------------------------------------------------------
// A stub for the browser's SpeechRecognition: records start/stop/abort and lets a test drive
// result events. The component reads `window.SpeechRecognition` lazily, so installing this before
// render makes the mic appear (and not installing it models an unsupported browser like Firefox).
type Seg = { transcript: string; isFinal: boolean };
let lastRecog: FakeRecognition | null = null;

class FakeRecognition {
  continuous = false;
  interimResults = false;
  lang = "";
  maxAlternatives = 1;
  onresult: ((ev: SpeechRecognitionEvent) => void) | null = null;
  onerror: ((ev: SpeechRecognitionErrorEvent) => void) | null = null;
  onend: ((ev: Event) => void) | null = null;
  onstart: ((ev: Event) => void) | null = null;
  start = vi.fn();
  stop = vi.fn(() => this.onend?.(new Event("end")));
  abort = vi.fn();
  constructor() {
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- test double exposes its instance
    lastRecog = this;
  }
  /** Drive a result event the way the engine would, from the current resultIndex. */
  emit(segments: Seg[], resultIndex = 0) {
    const results = segments.map((s) => ({
      0: { transcript: s.transcript, confidence: 1 },
      isFinal: s.isFinal,
      length: 1,
      item: () => ({ transcript: s.transcript, confidence: 1 }),
    }));
    this.onresult?.({ resultIndex, results } as unknown as SpeechRecognitionEvent);
  }
  /** Drive an error event (permission denied etc). */
  fail(error: string) {
    this.onerror?.({ error, message: error } as unknown as SpeechRecognitionErrorEvent);
  }
}

// getUserMedia is the reliable mic-grant path on Android (#659 follow-up): dictation now acquires
// the mic through it before building the recognizer. Default: resolve with a dummy stream; set
// `gumReject` to a DOMException name to model a denied / absent mic.
let gumReject: string | null = null;
const installSpeech = () => {
  window.SpeechRecognition = FakeRecognition as unknown as typeof window.SpeechRecognition;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: vi.fn(() =>
        gumReject
          ? Promise.reject(Object.assign(new Error(gumReject), { name: gumReject }))
          : Promise.resolve({ getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream),
      ),
    },
  });
};

// Tap the mic and wait for the async getUserMedia grant to spin up the recognizer. Uses the exact
// aria-label (not the /start voice input/i regex the tests use) so it isn't itself rewritten.
async function startVoice(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Start voice input" }));
  await waitFor(() => expect(lastRecog).not.toBeNull());
}

// #711: the snapshot collapse weighs per-entry arrival times read from performance.now(). Tests
// that model timing ("arrived at interim cadence" vs "spoken seconds later") drive this virtual
// clock; tests that don't call it use the real clock, where same-tick events land in-burst.
let perfSpy: { mockRestore(): void } | null = null;
const mockClock = () => {
  const clock = { now: 0 };
  perfSpy = vi.spyOn(performance, "now").mockImplementation(() => clock.now);
  return clock;
};

afterEach(() => {
  lastRecog = null;
  gumReject = null;
  perfSpy?.mockRestore();
  perfSpy = null;
  delete window.SpeechRecognition;
  delete window.webkitSpeechRecognition;
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
});

test("the mic chip is hidden when the browser has no SpeechRecognition (#483)", () => {
  renderCompose(); // no stub installed → unsupported engine, e.g. Firefox
  expect(screen.queryByRole("button", { name: /voice input/i })).not.toBeInTheDocument();
});

test("the mic chip renders when SpeechRecognition is available and is icon-only (#483)", () => {
  installSpeech();
  renderCompose();
  const mic = screen.getByRole("button", { name: /start voice input/i });
  expect(mic.textContent ?? "").toBe(""); // icon-only invariant: aria-label + title are the affordance
  expect(mic).toHaveAttribute("aria-label", expect.stringMatching(/voice input/i));
  expect(mic).toHaveAttribute("title", expect.stringMatching(/dictate/i));
  expect(mic).toHaveAttribute("aria-pressed", "false");
});

test("tapping the mic starts dictation, streams the transcript in, then tapping again stops (#483)", async () => {
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  await startVoice(user);
  expect(lastRecog).not.toBeNull();
  expect(lastRecog!.start).toHaveBeenCalled();
  expect(lastRecog!.continuous).toBe(true);
  expect(lastRecog!.interimResults).toBe(true);
  expect(screen.getByRole("button", { name: /voice input/i })).toHaveAttribute("aria-pressed", "true");

  act(() => lastRecog!.emit([{ transcript: "deploy the staging build", isFinal: true }]));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("deploy the staging build");

  await user.click(screen.getByRole("button", { name: /stop voice input/i }));
  expect(lastRecog!.stop).toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /voice input/i })).toHaveAttribute("aria-pressed", "false");
});

test("dictation appends to already-typed text and streams interim then final (#483)", async () => {
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await user.type(ta, "hello");
  await startVoice(user);
  act(() => lastRecog!.emit([{ transcript: "world", isFinal: false }]));
  expect(ta.value).toBe("hello world"); // interim shows live, appended after the typed text
  act(() => lastRecog!.emit([{ transcript: "world wide", isFinal: true }]));
  expect(ta.value).toBe("hello world wide"); // finalized result replaces the interim
});

test("a permission-denied error surfaces a note and leaves the mic idle (#483)", async () => {
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  await startVoice(user);
  act(() => lastRecog!.fail("not-allowed"));
  expect(await screen.findByText(/allow microphone/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /voice input/i })).toHaveAttribute("aria-pressed", "false");
});

test("collapsing the compose box stops an active dictation (#483)", async () => {
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  await startVoice(user);
  expect(lastRecog!.start).toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: /collapse compose/i }));
  expect(lastRecog!.stop).toHaveBeenCalled();
});

test("unmounting aborts an active dictation so no recognizer outlives the box (#483)", async () => {
  installSpeech();
  const user = userEvent.setup();
  const { unmount } = renderCompose();
  await startVoice(user);
  unmount();
  expect(lastRecog!.abort).toHaveBeenCalled();
});

test("a transcript re-fired many times (Chrome continuous mode) is NOT duplicated (#487)", async () => {
  // Chrome re-fires onresult repeatedly for the SAME finalized utterance; the handler must be
  // idempotent (rebuild from the cumulative results list), not accumulate → "said once, typed 10×".
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  for (let i = 0; i < 6; i++) {
    act(() => lastRecog!.emit([{ transcript: "deploy the staging build", isFinal: true }]));
  }
  expect(ta.value).toBe("deploy the staging build"); // once — never repeated
});

test("stacked interim snapshots (Android Chrome) collapse to the last one, not a growing prefix chain", async () => {
  // Android Chrome appends each interim snapshot as its OWN entry in `e.results` instead of
  // replacing the live one in place, so the list grows "this" / "this is" / "this is a" / … .
  // Concatenating the whole list types the prefix chain: "thisthis isthis is athis is a test".
  // Per spec only the LAST entry may be non-final, so stale non-final entries are dropped.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  act(() =>
    lastRecog!.emit([
      { transcript: "this", isFinal: false },
      { transcript: "this is", isFinal: false },
      { transcript: "this is a", isFinal: false },
      { transcript: "this is a test", isFinal: false },
    ]),
  );
  expect(ta.value).toBe("this is a test");
  // …and when the engine finalizes it on top of the stale interim stack, same answer.
  act(() =>
    lastRecog!.emit([
      { transcript: "this", isFinal: false },
      { transcript: "this is a", isFinal: false },
      { transcript: "this is a test", isFinal: true },
    ]),
  );
  expect(ta.value).toBe("this is a test");
});

test("multiple finalized utterances still concatenate in order (#487)", async () => {
  // The stale-interim guard must not swallow genuine multi-utterance finals, which are disjoint
  // segments rather than growing snapshots of one another.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  act(() =>
    lastRecog!.emit([
      { transcript: "deploy the build", isFinal: true },
      { transcript: " then run the tests", isFinal: true },
      { transcript: " and rep", isFinal: false },
    ]),
  );
  expect(ta.value).toBe("deploy the build then run the tests and rep");
});

test("replays the captured Android Chrome 150 session — types the sentence once (#711)", async () => {
  // The real device capture behind #711: 8 events, EVERY entry isFinal, not one interim, plus two
  // finalized empty strings. On v0.13.0 this typed
  // "this this is this is this is this is a this is a test" into the compose box.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  const captured = ["", "", "this", "this is", "this is", "this is", "this is a", "this is a test"];
  const capturedAtMs = [1931, 2192, 2519, 2733, 3059, 3459, 3620, 4107];
  const clock = mockClock();
  // Replay it the way the engine did: one appended entry per event, cumulative results each time,
  // at the timestamps the device actually delivered them.
  for (let n = 1; n <= captured.length; n++) {
    clock.now = capturedAtMs[n - 1];
    const slice = captured.slice(0, n).map((transcript) => ({ transcript, isFinal: true }));
    act(() => lastRecog!.emit(slice, n - 1));
  }
  expect(ta.value).toBe("this is a test");
});

test("a compliant engine's repeated utterance survives — 'yes' twice stays 'yes yes' (#711)", async () => {
  // The regression that blocked the first attempt at this fix. Modelled the way a compliant engine
  // actually behaves: each utterance is narrated through an interim entry first, so neither final
  // materialized pre-finalized — and an interim-born entry never supersedes its neighbour.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  act(() => lastRecog!.emit([{ transcript: "yes", isFinal: false }]));
  act(() => lastRecog!.emit([{ transcript: "yes", isFinal: true }]));
  act(() => lastRecog!.emit([{ transcript: "yes", isFinal: true }, { transcript: "yes", isFinal: false }]));
  act(() => lastRecog!.emit([{ transcript: "yes", isFinal: true }, { transcript: "yes", isFinal: true }]));
  expect(ta.value).toBe("yes yes"); // both utterances kept
});

test("a compliant engine's 'go' then 'go now' keeps both utterances (#711)", async () => {
  // The second final here is born-final (no interim of its own), so what protects it is timing:
  // a real follow-up utterance needs new speech + endpointing silence, which puts its final well
  // outside the snapshot burst window of the first.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 1000;
  act(() => lastRecog!.emit([{ transcript: "go", isFinal: false }]));
  clock.now = 1600;
  act(() => lastRecog!.emit([{ transcript: "go", isFinal: true }]));
  clock.now = 3600;
  act(() => lastRecog!.emit([{ transcript: "go", isFinal: true }, { transcript: "go now", isFinal: true }]));
  expect(ta.value).toBe("go go now");
});

test("a final-only engine's separate utterances survive when spoken apart (#711 round 4)", async () => {
  // No interim ever appears, yet nothing may be deleted: each utterance's final lands seconds
  // after the previous one (speech + endpointing), which is the evidence that it is real speech —
  // three genuine prefix-extending utterances, all kept.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 1000;
  act(() => lastRecog!.emit([{ transcript: "go", isFinal: true }]));
  clock.now = 3200;
  act(() => lastRecog!.emit([{ transcript: "go", isFinal: true }, { transcript: "go now", isFinal: true }]));
  clock.now = 5600;
  act(() =>
    lastRecog!.emit([
      { transcript: "go", isFinal: true },
      { transcript: "go now", isFinal: true },
      { transcript: "go now please", isFinal: true },
    ]),
  );
  expect(ta.value).toBe("go go now go now please");
});

test("one interim does not exempt later stacked all-final snapshots (#711 follow-up)", async () => {
  // The latch counterexample: a stacker that identified itself (finalized empty entry), emitted a
  // single interim, then keeps appending pre-finalized snapshots at interim cadence (one per
  // event, as the captured device does), must still collapse — the decision is per entry pair,
  // never a session-wide classification.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 700;
  act(() => lastRecog!.emit([{ transcript: "", isFinal: true }]));
  clock.now = 1000;
  act(() => lastRecog!.emit([{ transcript: "", isFinal: true }, { transcript: "this", isFinal: false }]));
  clock.now = 1300;
  act(() =>
    lastRecog!.emit([
      { transcript: "", isFinal: true },
      { transcript: "this", isFinal: true },
      { transcript: "this is", isFinal: true },
    ]),
  );
  clock.now = 1600;
  act(() =>
    lastRecog!.emit([
      { transcript: "", isFinal: true },
      { transcript: "this", isFinal: true },
      { transcript: "this is", isFinal: true },
      { transcript: "this is a test", isFinal: true },
    ]),
  );
  expect(ta.value).toBe("this is a test");
});

test("two finals first exposed by ONE event both survive — batched delivery (#711 round 5)", async () => {
  // A service may buffer and deliver two genuine finals in a single onresult. They share one
  // sampled timestamp, which is no evidence of snapshot cadence — both must be typed, even though
  // their texts are prefix-related.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 1000;
  act(() =>
    lastRecog!.emit([
      { transcript: "go", isFinal: true },
      { transcript: "go now", isFinal: true },
    ]),
  );
  expect(ta.value).toBe("go go now");
});

test("an interim-tracked utterance followed by a real second one keeps both (#711)", async () => {
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 1000;
  act(() => lastRecog!.emit([{ transcript: "ship", isFinal: false }]));
  clock.now = 1600;
  act(() => lastRecog!.emit([{ transcript: "ship", isFinal: true }]));
  clock.now = 3600;
  act(() => lastRecog!.emit([{ transcript: "ship", isFinal: true }, { transcript: "ship it", isFinal: true }]));
  expect(ta.value).toBe("ship ship it"); // two real utterances, seconds apart
});

test("a re-punctuated snapshot supersedes its predecessor on a stacking engine (#711)", async () => {
  // Drip shape as captured: the stacker finalizes an empty entry (its fingerprint), then appends
  // one pre-finalized snapshot per event at interim cadence.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  const snapshots = ["", "hey claude", "Hey, Claude — can you", "Hey, Claude, can you deploy?"];
  for (let n = 1; n <= snapshots.length; n++) {
    clock.now = 1000 + n * 300;
    act(() => lastRecog!.emit(snapshots.slice(0, n).map((transcript) => ({ transcript, isFinal: true }))));
  }
  expect(ta.value).toBe("Hey, Claude, can you deploy?");
});

test("a distinct utterance spoken after a pause is never swallowed by the collapse (#711)", async () => {
  // "and" then "android studio" as two real utterances: the second final arrives well outside the
  // burst window, so the trailing-word revision rule ("and" → "android") does not apply.
  installSpeech();
  const clock = mockClock();
  const user = userEvent.setup();
  renderCompose();
  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await startVoice(user);
  clock.now = 1000;
  act(() => lastRecog!.emit([{ transcript: "and", isFinal: true }]));
  clock.now = 3000;
  act(() =>
    lastRecog!.emit([
      { transcript: "and", isFinal: true },
      { transcript: "android studio", isFinal: true },
    ]),
  );
  expect(ta.value).toBe("and android studio");
});

test("a denied getUserMedia grant names the reason and never builds a recognizer (#659 follow-up)", async () => {
  // The reliable Android mic path: if getUserMedia is refused, we surface an actionable note and
  // never start a recognizer (so there's nothing to leak) — instead of the old silent "blocked".
  installSpeech();
  gumReject = "NotAllowedError";
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: "Start voice input" }));
  expect(await screen.findByText(/allow microphone/i)).toBeInTheDocument();
  expect(lastRecog).toBeNull(); // grant refused up front — no recognizer created
  expect(screen.getByRole("button", { name: /voice input/i })).toHaveAttribute("aria-pressed", "false");
});

test("Android Chrome's service-not-allowed on a continuous recognizer retries once, non-continuous", async () => {
  // Android Chrome rejects a continuous recognizer with `service-not-allowed`; we fall back once to
  // a single-utterance recognizer rather than surfacing an error.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  await startVoice(user);
  const first = lastRecog!;
  expect(first.continuous).toBe(true);
  act(() => first.fail("service-not-allowed"));
  expect(lastRecog).not.toBe(first); // a fresh recognizer took over synchronously
  expect(lastRecog!.continuous).toBe(false);
  expect(lastRecog!.start).toHaveBeenCalled();
  expect(screen.queryByText(/unavailable|blocked|error/i)).not.toBeInTheDocument();
});

test("a getUserMedia grant resolving AFTER unmount builds no stale recognizer (#660 review)", async () => {
  // The async twin of the stop guard: tap the mic, unmount before the grant resolves, then resolve
  // it. The unmount cleanup must invalidate the pending grant so no recognizer is created after the
  // Compose box is gone.
  installSpeech();
  let resolveGrant!: (s: unknown) => void;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn(() => new Promise((r) => (resolveGrant = r))) },
  });
  const user = userEvent.setup();
  const { unmount } = renderCompose();
  await user.click(screen.getByRole("button", { name: "Start voice input" }));
  expect(lastRecog).toBeNull(); // grant still pending — no recognizer yet
  unmount();
  await act(async () => {
    resolveGrant({ getTracks: () => [{ stop: vi.fn() }] });
  });
  expect(lastRecog).toBeNull(); // the post-unmount grant must NOT spin up a recognizer
});

test("an unmapped speech error names itself instead of a generic 'microphone blocked' (#659 follow-up)", async () => {
  // The whole point of the follow-up: never hide the real failure behind a generic string again.
  installSpeech();
  const user = userEvent.setup();
  renderCompose();
  await startVoice(user);
  act(() => lastRecog!.fail("some-odd-code"));
  expect(await screen.findByText(/voice input error: some-odd-code/i)).toBeInTheDocument();
});

test("the nav-key chips send their control sequence to the PTY (#487/#500)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: "Up" }));
  await user.click(screen.getByRole("button", { name: "Down" }));
  await user.click(screen.getByRole("button", { name: "Return" }));
  await user.click(screen.getByRole("button", { name: "Tab" }));
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.up);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.down);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter);
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.tab);
});

test("the single-row bar has no second (kebab) menu and no copy / interrupt chips (#500/#503)", () => {
  renderCompose();
  expect(screen.queryByRole("button", { name: /more actions/i })).not.toBeInTheDocument(); // no kebab
  expect(screen.queryByRole("button", { name: /interrupt|ctrl-c/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
  // The chips are the nav group (up/down/return/esc/tab) + attach + close, then the inline Send.
  expect(screen.getByRole("button", { name: "Up" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Escape" })).toBeInTheDocument(); // esc re-added (#503)
  expect(screen.getByRole("button", { name: /attach file/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /collapse compose/i })).toBeInTheDocument();
});

test("the esc chip sends the escape sequence to the PTY (#503)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: "Escape" }));
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.esc);
});

test("the attach chip lives in the key group and triggers the file input (#487/#500)", async () => {
  const user = userEvent.setup();
  renderCompose();
  const attach = screen.getByRole("button", { name: /attach file/i });
  // Icon-only affordance (no visible text).
  expect(attach.textContent ?? "").toBe("");
  // Clicking it opens the (hidden) native file picker — assert it forwards the click.
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const clicked = vi.spyOn(input, "click").mockImplementation(() => {});
  await user.click(attach);
  expect(clicked).toHaveBeenCalledOnce();
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
  // Compose reads connEpoch() once in deliver() and again inside the DEFERRED Enter — so a counter
  // makes the reconnect land in that gap by construction. Flipping a variable after `await click()`
  // instead assumed the 60ms Enter timer had not fired yet, which is only true on an idle machine:
  // on the loaded shared runner it had, and this test failed with "called 2 times, but got 3".
  let epochReads = 0;
  renderCompose(() => (++epochReads === 1 ? 1 : 2));
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // clear + paste reached socket A first, whatever the scheduler did with the deferred Enter.
  expect(sendInput.mock.calls.slice(0, 2).map((c) => c[0])).toEqual([
    KEYSEQ.ctrla + KEYSEQ.ctrlk,
    bracketedPaste("hello world"),
  ]);
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
  // As above: the reconnect is keyed to connEpoch's SECOND read (inside the deferred Enter), not to
  // wall-clock ordering, so a slow runner can't let the Enter slip through before the epoch flips.
  let epochReads = 0;
  renderCompose(() => (++epochReads === 1 ? 1 : 2));
  const ta = screen.getByRole("textbox");
  await user.type(ta, "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  await waitFor(() => expect((ta as HTMLTextAreaElement).value).toBe("hello world")); // text restored
  expect(sendInput).not.toHaveBeenCalledWith(KEYSEQ.enter);
});

test("a content send whose deferred Enter never delivers preserves + re-saves the draft (#477)", async () => {
  // Hermes #480: the composer + server draft must NOT be cleared until the deferred Enter is
  // confirmed delivered. If that final frame drops, the turn was never submitted — keep the text
  // AND re-persist the draft so a reload / session switch doesn't silently lose it.
  const user = userEvent.setup();
  sendInput = vi.fn((d) => d !== KEYSEQ.enter); // clear + paste deliver; the bare Enter does NOT
  render(
    <Compose sessionId="claude:s1" sendInput={sendInput} connEpoch={() => 1} />,
  );
  const ta = screen.getByRole("textbox");
  await user.type(ta, "keep me");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter)); // deferred Enter attempted
  await waitFor(() => expect((ta as HTMLTextAreaElement).value).toBe("keep me")); // composer kept
  const saved = vi.mocked(api.saveDraft).mock.calls.map((c) => c[1]);
  expect(saved.some((d) => d.text === "keep me")).toBe(true); // restored content persisted
  expect(saved.some((d) => d.text === "" && d.attachments.length === 0)).toBe(false); // never cleared
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
  render(<Compose ref={handle} sendInput={sendInput} />);
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

test("attachImages opens the compose (if collapsed) and adds the upload as a pill (#157)", async () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  vi.mocked(api.upload).mockResolvedValue({ name: "shot.png", path: "/uploads/shot.png" });
  const ref = createRef<ComposeHandle>();
  render(<Compose ref={ref} sendInput={sendInput} defaultOpen={false} />);
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
  render(<Compose ref={ref} sendInput={sendInput} defaultOpen={false} />);
  ref.current!.attachImages([]);
  expect(api.upload).not.toHaveBeenCalled();
});

// --- Fresh-launch readiness gate (#533) ---------------------------------------------------------
// Input written into a still-booting agent is swallowed (the composed text) or mis-submitted (the
// production incident's first turn was the literal Ctrl-A of the compose clear). The content send
// must hold until the terminal reports the agent's input live, and give up non-destructively.

test("holds the first Send until the agent's input is ready, then delivers (#533)", async () => {
  const user = userEvent.setup();
  let resolveReady!: (ok: boolean) => void;
  const waitInputReady = vi.fn(
    (): true | Promise<boolean> => new Promise<boolean>((r) => (resolveReady = r)),
  );
  render(<Compose sendInput={sendInput} connEpoch={() => 1} waitInputReady={waitInputReady} />);
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // Nothing may reach the PTY while the agent is booting — the lost-first-message incident was
  // exactly these frames landing inside the boot window.
  expect(sendInput).not.toHaveBeenCalled();
  expect(screen.getByText(/waiting for agent/i)).toBeTruthy();
  act(() => resolveReady(true));
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(bracketedPaste("hello world")));
  expect(sendInput).toHaveBeenNthCalledWith(1, KEYSEQ.ctrla + KEYSEQ.ctrlk);
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter)); // deferred Enter intact
});

test("ready-before-send (synchronous true) keeps the delivery sequence unchanged (#533)", async () => {
  const user = userEvent.setup();
  render(<Compose sendInput={sendInput} connEpoch={() => 1} waitInputReady={() => true} />);
  await user.type(screen.getByRole("textbox"), "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  // Identical to the ungated path: clear + paste synchronously, Enter deferred (#180).
  expect(sendInput).toHaveBeenNthCalledWith(1, KEYSEQ.ctrla + KEYSEQ.ctrlk);
  expect(sendInput).toHaveBeenNthCalledWith(2, bracketedPaste("hello world"));
  expect(sendInput).toHaveBeenCalledTimes(2);
  await waitFor(() => expect(sendInput).toHaveBeenNthCalledWith(3, KEYSEQ.enter));
});

test("readiness timeout never sends, keeps the text, and says why (#533)", async () => {
  const user = userEvent.setup();
  render(
    <Compose sendInput={sendInput} connEpoch={() => 1} waitInputReady={() => Promise.resolve(false)} />,
  );
  const ta = screen.getByRole("textbox");
  await user.type(ta, "hello world");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  await waitFor(() => expect(screen.getByText(/agent not ready/i)).toBeTruthy());
  expect(sendInput).not.toHaveBeenCalled(); // no frame ever reached the booting agent
  expect((ta as HTMLTextAreaElement).value).toBe("hello world"); // preserved for a retry
});

// --- Sent-message history (#619) ---------------------------------------------------------------
// The safety net for a send the agent swallows (#616): every submission is recorded BEFORE the
// composer and the server draft are cleared, so it stays recoverable even when delivery "succeeds".

test("a send is recorded BEFORE the composer clears, and confirmed only once the Enter lands (#619)", async () => {
  const user = userEvent.setup();
  render(<Compose sessionId="claude:s1" sendInput={sendInput} connEpoch={() => 1} />);
  await user.type(screen.getByRole("textbox"), "recover me");
  await user.click(screen.getByRole("button", { name: /^send/i }));

  // Recorded at submit time — unconfirmed until the deferred Enter is delivered.
  expect(readSent().map((e) => e.text)).toEqual(["recover me"]);
  expect(readSent()[0].session).toBe("claude:s1");

  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter));
  await waitFor(() => expect(readSent()[0].confirmed).toBe(true));
});

test("a send whose Enter never reaches the socket stays UNCONFIRMED (#619)", async () => {
  const user = userEvent.setup();
  // clear + paste deliver; the deferred Enter does not (a reconnect landed in the gap, #287).
  sendInput = vi.fn((d: string) => d !== KEYSEQ.enter);
  render(<Compose sessionId="claude:s1" sendInput={sendInput} connEpoch={() => 1} />);
  await user.type(screen.getByRole("textbox"), "never landed");
  await user.click(screen.getByRole("button", { name: /^send/i }));

  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter));
  expect(readSent()[0].text).toBe("never landed");
  expect(readSent()[0].confirmed).toBe(false); // the abort path leaves it recoverable + flagged
});

test("an empty Send (bare Return) records nothing (#619 / #474)", async () => {
  const user = userEvent.setup();
  renderCompose();
  await user.click(screen.getByRole("button", { name: /^send/i }));
  expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter);
  expect(readSent()).toEqual([]); // a bare Return is not a message
});

test("the history chip is hidden until there is something to recover (#619)", async () => {
  const user = userEvent.setup();
  renderCompose();
  expect(screen.queryByRole("button", { name: /sent messages/i })).toBeNull();
  await user.type(screen.getByRole("textbox"), "first message");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /sent messages/i })).toBeTruthy());
});

test("Restore refills the composer with the exact text + attachments, and flushes the draft (#619)", async () => {
  const user = userEvent.setup();
  appendSent({ text: "  keep my\n\nwhitespace  ", attachments: ["/up/a.png"], session: "claude:s1" });
  render(<Compose sessionId="claude:s1" sendInput={sendInput} connEpoch={() => 1} />);

  await user.click(screen.getByRole("button", { name: /sent messages/i }));
  await user.click(screen.getByRole("button", { name: /restore/i }));

  const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
  await waitFor(() => expect(ta.value).toBe("  keep my\n\nwhitespace  ")); // untrimmed round-trip
  expect(screen.getByTitle("/up/a.png")).toBeTruthy(); // the attachment pill is back
  // Hermes: Restore must share typing's dirty + flushDraft path, or a refresh loses it again.
  await waitFor(() =>
    expect(api.saveDraft).toHaveBeenCalledWith("claude:s1", {
      text: "  keep my\n\nwhitespace  ",
      attachments: [{ name: "a.png", path: "/up/a.png" }],
    }),
  );
});

test("a failing localStorage never blocks the send (#619)", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {
      throw new DOMException("QuotaExceededError");
    },
    removeItem: () => {},
  });
  renderCompose();
  await user.type(screen.getByRole("textbox"), "still sends");
  await user.click(screen.getByRole("button", { name: /^send/i }));
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(bracketedPaste("still sends")));
  await waitFor(() => expect(sendInput).toHaveBeenCalledWith(KEYSEQ.enter));
  vi.unstubAllGlobals();
});
