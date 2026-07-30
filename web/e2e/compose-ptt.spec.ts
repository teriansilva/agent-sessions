import { expect, test } from "@playwright/test";

// #483: push-to-talk voice dictation in the compose box. Headless Chromium can't capture real
// audio, so window.SpeechRecognition is stubbed to emit a canned interim+final transcript on
// start(); the test drives the REAL UI — tap the mic, the transcript streams into the textarea,
// tap again to stop. Runs under both the desktop and mobile Playwright projects (see config).
//
// Red-before/green-after: before this feature there is no "voice input" control, so the
// getByRole(...).click() below has nothing to act on and the test fails on the prior code.

// A SpeechRecognition stub that records start/stop and emits an interim then a final transcript —
// and then RE-FIRES the final several times, the way Chrome's continuous mode re-delivers a
// finalized utterance (#487). The handler must be idempotent: the transcript lands once, not N×.
const SPEECH_STUB = `
window.__recog = { started: 0, stopped: 0 };
window.SpeechRecognition = class {
  constructor() {
    this.continuous = false; this.interimResults = false; this.lang = "";
    this.onresult = null; this.onerror = null; this.onend = null;
    window.__recog.instance = this;
  }
  start() {
    window.__recog.started++;
    const interim = { resultIndex: 0,
      results: [{ 0: { transcript: "deploy the staging build" }, isFinal: false, length: 1 }] };
    const finalEv = { resultIndex: 0,
      results: [{ 0: { transcript: "deploy the staging build and watch the rollout" }, isFinal: true, length: 1 }] };
    setTimeout(() => { if (this.onresult) this.onresult(interim); }, 30);
    // Re-fire the SAME finalized result 5× — the duplication trigger (#487).
    [80, 110, 140, 170, 200].forEach((t) =>
      setTimeout(() => { if (this.onresult) this.onresult(finalEv); }, t));
  }
  stop() { window.__recog.stopped++; if (this.onend) this.onend(); }
  abort() { if (this.onend) this.onend(); }
};
`;

// #736: a recognizer that ends its OWN session after every utterance — Android Chrome's
// single-utterance fallback (`continuous = false`), and equally Chrome's endpointer hanging up on a
// pause. Sessions 1 and 2 each speak one phrase and then fire `end`; from session 3 on the stub
// simply stays open and silent, the way a live engine waits for more speech. Before the fix the
// first `end` tore dictation down, so phrase 2 was never heard — "records a part, then stops".
const SPEECH_STUB_ENGINE_HANGUP = `
window.__recog = { started: 0, stopped: 0 };
window.SpeechRecognition = class {
  constructor() {
    this.continuous = false; this.interimResults = false; this.lang = "";
    this.onresult = null; this.onerror = null; this.onend = null;
    window.__recog.instance = this;
  }
  start() {
    const n = ++window.__recog.started;
    const phrase = ["deploy the staging build", "and watch the rollout"][n - 1];
    if (!phrase) return; // session 3+: armed and listening, nothing more to say
    setTimeout(() => { if (this.onresult) this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: phrase }, isFinal: true, length: 1 }] }); }, 30);
    setTimeout(() => { if (this.onend) this.onend(); }, 60); // the engine hangs up
  }
  stop() { window.__recog.stopped++; if (this.onend) this.onend(); }
  abort() { if (this.onend) this.onend(); }
};
`;

// Dictation now acquires the mic via getUserMedia BEFORE building the recognizer (#659 follow-up:
// the reliable Android grant path). Headless Chromium has no real audio device, so stub it to
// resolve — otherwise the grant rejects and the stubbed recognizer never starts.
const GUM_STUB = `
Object.defineProperty(navigator, "mediaDevices", {
  configurable: true,
  value: { getUserMedia: () => Promise.resolve({ getTracks: () => [{ stop() {} }] }) },
});
`;

// No-op WebSocket so the terminal mounts without a backend (E2E serves the static SPA only).
const NOOP_WS = `
window.WebSocket = class {
  constructor() { this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 20); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

test("push-to-talk streams the transcript into the compose box; tapping again stops (#483)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-483");
  await expect(page.locator(".xterm")).toBeVisible();

  // The mic chip only renders when the compose box is open (collapsed by default on desktop, open
  // on mobile). Open it if needed.
  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();

  const textarea = page.getByPlaceholder(/Type here/i); // the compose textarea, not the xterm one
  await expect(textarea).toHaveValue("");

  // Tap the mic → the stub streams interim then final transcript into the textarea.
  await mic.click();
  await expect(textarea).toHaveValue(/deploy the staging build/); // interim arrives first
  await expect(textarea).toHaveValue("deploy the staging build and watch the rollout"); // final
  // The final result re-fires 5× (Chrome behaviour, #487). After they all land the transcript must
  // appear exactly ONCE — never duplicated. Wait out the re-fires, then assert the exact value.
  await page.waitForTimeout(250);
  await expect(textarea).toHaveValue("deploy the staging build and watch the rollout");
  // While recording the control is the "stop" affordance and reports its pressed state.
  const stopMic = page.getByRole("button", { name: /stop voice input/i });
  await expect(stopMic).toHaveAttribute("aria-pressed", "true");

  // Tap again → stops, back to the idle "start" affordance.
  await stopMic.click();
  await expect(page.getByRole("button", { name: /start voice input/i })).toHaveAttribute(
    "aria-pressed",
    "false",
  );

  const recog = await page.evaluate(
    () => (window as unknown as { __recog: { started: number; stopped: number } }).__recog,
  );
  expect(recog.started).toBe(1);
  expect(recog.stopped).toBe(1);
});

test("dictation survives the engine ending its own session — both utterances land (#736)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB_ENGINE_HANGUP);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-736");
  await expect(page.locator(".xterm")).toBeVisible();

  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();

  const textarea = page.getByPlaceholder(/Type here/i);
  await expect(textarea).toHaveValue("");

  // One tap. The engine hangs up after the first phrase; dictation must re-arm itself and keep the
  // second one — red before the fix, where the textarea stopped at "deploy the staging build".
  await mic.click();
  await expect(textarea).toHaveValue("deploy the staging build");
  await expect(textarea).toHaveValue("deploy the staging build and watch the rollout");

  // Still listening after both hang-ups — only the user ends dictation. Poll for the third session:
  // the second phrase lands BEFORE its session hangs up, so reading the count straight after the
  // transcript assertion would race the re-arm rather than test it.
  const started = () =>
    page.evaluate(() => (window as unknown as { __recog: { started: number } }).__recog.started);
  await expect.poll(started).toBe(3);
  const stopMic = page.getByRole("button", { name: /stop voice input/i });
  await expect(stopMic).toHaveAttribute("aria-pressed", "true");

  await stopMic.click();
  await expect(page.getByRole("button", { name: /start voice input/i })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  // …and the stop is final: no re-arm after the user's own stop.
  await page.waitForTimeout(200);
  expect(await started()).toBe(3);
});
