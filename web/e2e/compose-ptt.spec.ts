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

test("push-to-talk streams the transcript into the compose box; releasing stops (#483/#738)", async ({
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

  // HOLD the mic (#738 — it is no longer a toggle) → the stub streams interim then final.
  await mic.hover();
  await page.mouse.down();
  await expect(textarea).toHaveValue(/deploy the staging build/); // interim arrives first
  await expect(textarea).toHaveValue(
    "deploy the staging build and watch the rollout",
  ); // final
  // The final result re-fires 5× (Chrome behaviour, #487). After they all land the transcript must
  // appear exactly ONCE — never duplicated. Wait out the re-fires, then assert the exact value.
  await page.waitForTimeout(250);
  await expect(textarea).toHaveValue(
    "deploy the staging build and watch the rollout",
  );
  // While recording the control is the "stop" affordance and reports its pressed state.
  const stopMic = page.getByRole("button", { name: /stop voice input/i });
  await expect(stopMic).toHaveAttribute("aria-pressed", "true");

  // Let go → stops, back to the idle "start" affordance.
  await page.mouse.up();
  await expect(
    page.getByRole("button", { name: /start voice input/i }),
  ).toHaveAttribute("aria-pressed", "false");

  const recog = await page.evaluate(
    () =>
      (window as unknown as { __recog: { started: number; stopped: number } })
        .__recog,
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

  // One HOLD. The engine hangs up after the first phrase; dictation must re-arm itself and keep the
  // second one — red before the fix, where the textarea stopped at "deploy the staging build".
  await mic.hover();
  await page.mouse.down();
  await expect(textarea).toHaveValue("deploy the staging build");
  await expect(textarea).toHaveValue(
    "deploy the staging build and watch the rollout",
  );

  // Still listening after both hang-ups — only the user ends dictation. Poll for the third session:
  // the second phrase lands BEFORE its session hangs up, so reading the count straight after the
  // transcript assertion would race the re-arm rather than test it.
  const started = () =>
    page.evaluate(
      () =>
        (window as unknown as { __recog: { started: number } }).__recog.started,
    );
  await expect.poll(started).toBe(3);
  const stopMic = page.getByRole("button", { name: /stop voice input/i });
  await expect(stopMic).toHaveAttribute("aria-pressed", "true");

  await page.mouse.up();
  await expect(
    page.getByRole("button", { name: /start voice input/i }),
  ).toHaveAttribute("aria-pressed", "false");
  // …and the release is final: no re-arm after the user let go.
  await page.waitForTimeout(200);
  expect(await started()).toBe(3);
});

// #738: the mic is a press-and-HOLD, and releasing must finish the transcription rather than bin
// it. This stub models the real contract of `stop()`: capture ends, then the engine delivers what
// it already heard. `__recog.tail` fires that trailing final on demand so the test can prove it
// lands AFTER the release.
const SPEECH_STUB_HOLD = `
window.__recog = { started: 0, stopped: 0, aborted: 0 };
window.SpeechRecognition = class {
  constructor() {
    this.continuous = false; this.interimResults = false; this.lang = "";
    this.onresult = null; this.onerror = null; this.onend = null;
    window.__recog.instance = this;
  }
  start() {
    window.__recog.started++;
    setTimeout(() => { if (this.onresult) this.onresult({ resultIndex: 0,
      results: [{ 0: { transcript: "hold to dictate" }, isFinal: false, length: 1 }] }); }, 30);
  }
  // stop() ends capture only — the tail is still owed, exactly like a real engine.
  stop() { window.__recog.stopped++; }
  abort() { window.__recog.aborted++; if (this.onend) this.onend(); }
};
window.__recog.tail = () => {
  const r = window.__recog.instance;
  if (r && r.onresult) r.onresult({ resultIndex: 0,
    results: [{ 0: { transcript: "hold to dictate the whole sentence" }, isFinal: true, length: 1 }] });
  if (r && r.onend) r.onend();
};
`;

test("press-and-hold records; releasing stops capture but still lands the tail (#738)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB_HOLD);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-738");
  await expect(page.locator(".xterm")).toBeVisible();

  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();
  // The chip now says what it is — a bare icon can't tell you to hold it.
  await expect(mic).toContainText(/push to talk/i);

  const textarea = page.getByPlaceholder(/Type here/i);
  await expect(textarea).toHaveValue("");

  // HOLD — press and keep the button down. A click (press+release) would be a whole dictation.
  await mic.hover();
  await page.mouse.down();
  await expect(
    page.getByRole("button", { name: /stop voice input/i }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(textarea).toHaveValue("hold to dictate"); // streaming while held

  // RELEASE — capture stops, but the engine still owes us the finished phrase.
  await page.mouse.up();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __recog: { stopped: number; aborted: number } })
          .__recog.stopped,
    ),
  ).toBe(1);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __recog: { aborted: number } }).__recog.aborted,
    ),
  ).toBe(0);

  // …and it arrives after the release. Red before #738: the old teardown detached onresult on stop,
  // so this final landed nowhere and the box stayed at "hold to dictate".
  await page.evaluate(() =>
    (window as unknown as { __recog: { tail: () => void } }).__recog.tail(),
  );
  await expect(textarea).toHaveValue("hold to dictate the whole sentence");
  await expect(
    page.getByRole("button", { name: /start voice input/i }),
  ).toHaveAttribute("aria-pressed", "false");

  // The labelled chip must not wrap the action bar — it shares the row with the key chips, which
  // collapse into the "…" overflow instead (#234). One row, both viewports.
  const bar = page.locator("button", { hasText: /push to talk/i }).first();
  const box = await bar.boundingBox();
  const send = await page.getByRole("button", { name: /^send/i }).boundingBox();
  expect(box).not.toBeNull();
  expect(send).not.toBeNull();
  expect(Math.abs(box!.y - send!.y)).toBeLessThan(box!.height); // same row, not stacked
});

test("holding Space outside a text field dictates; inside it types (#738)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB_HOLD);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-738-space");
  await expect(page.locator(".xterm")).toBeVisible();

  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();
  const textarea = page.getByPlaceholder(/Type here/i);

  // Space with focus in the compose box types a space — it must never be stolen.
  await textarea.click();
  await page.keyboard.press(" ");
  await expect(textarea).toHaveValue(" ");
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __recog: { started: number } }).__recog.started,
    ),
  ).toBe(0);

  // Space held on the page background is push-to-talk.
  await textarea.evaluate((el: HTMLElement) => el.blur());
  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await page.keyboard.down(" ");
  await expect(
    page.getByRole("button", { name: /stop voice input/i }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.up(" ");
  await expect(
    page.getByRole("button", { name: /start voice input/i }),
  ).toHaveAttribute("aria-pressed", "false");
});

test("a Space press cannot steal a pointer-owned hold or bin its tail (#738)", async ({
  page,
}) => {
  // The cross-input ownership case Hermes reproduced in a browser: with the chip held by the
  // pointer, pressing Space used to stop the live recognizer (`__recog.stopped` 0 → 1) and start a
  // replacement, losing the phrase being finalized.
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB_HOLD);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-738-owner");
  await expect(page.locator(".xterm")).toBeVisible();

  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();
  const textarea = page.getByPlaceholder(/Type here/i);

  // Focus somewhere that does not claim Space — Hermes's exact setup.
  await page.evaluate(() =>
    (document.activeElement as HTMLElement | null)?.blur(),
  );

  await mic.hover();
  await page.mouse.down();
  await expect(
    page.getByRole("button", { name: /stop voice input/i }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(textarea).toHaveValue("hold to dictate");

  await page.keyboard.down(" ");
  await page.keyboard.up(" ");
  const recog = await page.evaluate(
    () =>
      (window as unknown as { __recog: { started: number; stopped: number } })
        .__recog,
  );
  expect(recog.stopped).toBe(0); // the held session survived the keypress
  expect(recog.started).toBe(1); // …and no replacement was started
  await expect(
    page.getByRole("button", { name: /stop voice input/i }),
  ).toHaveAttribute("aria-pressed", "true");

  // The owning pointer still ends it, and the tail still lands.
  await page.mouse.up();
  await page.evaluate(() =>
    (window as unknown as { __recog: { tail: () => void } }).__recog.tail(),
  );
  await expect(textarea).toHaveValue("hold to dictate the whole sentence");
});

// #749: the operator's real device capture (Android 10 / Chrome 150, continuous, one session),
// replayed at its true relative timings so the burst window is exercised against a real clock.
// Every entry but the last is born final; three are finalized EMPTY (the #711 stacker fingerprint);
// the last is the LIVE phrase — it arrives as an interim and keeps growing. On v0.15.0 that last
// entry could not absorb its predecessor, so the sentence was typed twice.
const SPEECH_STUB_CAPTURE = `
window.__recog = { started: 0, stopped: 0 };
window.SpeechRecognition = class {
  constructor() {
    this.continuous = false; this.interimResults = false; this.lang = "";
    this.onresult = null; this.onerror = null; this.onend = null;
    window.__recog.instance = this;
  }
  start() {
    window.__recog.started++;
    const EV = [
      [1949,0,true,""],[2248,1,true,""],[2457,2,true,""],
      [2471,3,true,"and"],[2556,4,true,"and"],[2657,5,true,"and"],
      [2765,6,true,"and when"],[2969,7,true,"and when"],
      [3074,8,true,"and when you"],[3246,9,true,"and when you are"],
      [3286,10,true,"and when you are done"],[3378,11,true,"and when you are done"],
      [3482,12,true,"and when you are done with"],[3688,13,true,"and when you are done with"],
      [3799,14,true,"and when you are done with"],
      [4125,15,false,"and when you are done with all"],
      [4307,15,false,"and when you are done with all of"],
      [4313,15,false,"and when you are done with all of this"],
      [4319,15,true,"and when you are done with all of this"],
    ];
    const t0 = EV[0][0];
    const live = new Map();
    EV.forEach(([t, i, isFinal, transcript]) => {
      setTimeout(() => {
        live.set(i, { isFinal, transcript });
        const results = [...live.keys()].sort((a, b) => a - b).map((k) => ({
          0: { transcript: live.get(k).transcript },
          isFinal: live.get(k).isFinal,
          length: 1,
        }));
        if (this.onresult) this.onresult({ resultIndex: 0, results });
      }, t - t0);
    });
  }
  stop() { window.__recog.stopped++; if (this.onend) this.onend(); }
  abort() { if (this.onend) this.onend(); }
};
`;

test("the captured device stream types the sentence ONCE (#749)", async ({
  page,
}) => {
  await page.addInitScript(NOOP_WS);
  await page.addInitScript(SPEECH_STUB_CAPTURE);
  await page.addInitScript(GUM_STUB);
  await page.goto("/s/claude/ptt-749");
  await expect(page.locator(".xterm")).toBeVisible();

  const mic = page.getByRole("button", { name: /start voice input/i });
  if (!(await mic.isVisible())) {
    await page.getByRole("button", { name: /open compose box/i }).click();
  }
  await expect(mic).toBeVisible();
  const textarea = page.getByPlaceholder(/Type here/i);

  await mic.hover();
  await page.mouse.down();
  // The whole capture spans ~2.4s of wall clock; wait it out, then let go.
  await expect(textarea).toHaveValue("and when you are done with all of this");
  await page.mouse.up();

  // Red before #749: "and when you are done with and when you are done with all of this".
  await expect(textarea).toHaveValue("and when you are done with all of this");
});
