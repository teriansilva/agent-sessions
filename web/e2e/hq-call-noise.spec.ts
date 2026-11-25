/** #1667 — the call's noise gate in a real browser: Chromium's fake microphone fed a file of a
 *  near-silent room, through the browser's own noise suppression and gain control. Only silence
 *  frames reach the server, so its voice detector never hears the room. (A louder steady noise
 *  passes by design, to the worker's VAD: at -45 dBFS 191 frames carried sound here.) */
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { FAKE_MIC, connect, mockShell, say } from "./callFixture";

/** 10 s of steady white noise at about -65 dBFS (a quiet room), 48 kHz mono PCM16; Chromium
 *  loops the file. */
function roomNoiseWav(): string {
  const rate = 48_000;
  const n = rate * 10;
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0);
  buf.writeUInt32LE(36 + n * 2, 4);
  buf.write("WAVEfmt ", 8);
  buf.writeUInt32LE(16, 16);
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(1, 22); // mono
  buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28);
  buf.writeUInt16LE(2, 32);
  buf.writeUInt16LE(16, 34);
  buf.write("data", 36);
  buf.writeUInt32LE(n * 2, 40);
  let seed = 7;
  const amp = Math.pow(10, -65 / 20) * Math.sqrt(3) * 0x7fff;
  for (let i = 0; i < n; i++) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    buf.writeInt16LE(Math.round((seed / 0x100000000 - 0.5) * 2 * amp), 44 + i * 2);
  }
  const path = join(tmpdir(), `bl-room-noise-${process.pid}.wav`);
  writeFileSync(path, buf);
  return path;
}

// A file of room noise as the microphone: launch options are per file in Playwright, so this case
// has a file of its own (the sound case is in `hq-call.spec.ts`, on the default fake device).
test.use({
  ...FAKE_MIC,
  launchOptions: {
    // Repeated, not only spread: this replaces the config's launch options, and Chromium's
    // shared memory must stay out of the temp dir (tests/test_e2e_preview_lifecycle.py).
    ignoreDefaultArgs: ["--disable-dev-shm-usage"],
    args: [...FAKE_MIC.launchOptions.args, `--use-file-for-fake-audio-capture=${roomNoiseWav()}`],
  },
});

test("a near-silent room reaches the server only as silence frames", async ({ page }) => {
  const srv = await mockShell(page);
  await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  // Frames keep flowing (the worker's clock keeps its cadence)…
  await expect.poll(() => srv.binaries[0], { timeout: 15_000 }).toBeGreaterThan(150);
  // …but not one of them carries the room.
  expect(srv.voiced[0]).toBe(0);
});
