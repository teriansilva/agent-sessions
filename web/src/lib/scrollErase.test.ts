import { describe, expect, test } from "vitest";

import { resetRoster, wipesOnRepaint } from "../app/engineRoster";
import { createScrollEraseStripper } from "./scrollErase";

/** #600 (codex) / #1038 (kimi): the live-stream CSI 3J strip, extracted from Terminal.tsx with
 *  its across-chunk carry. The server applies the identical filter (scrollback.sanitize_live_output);
 *  these tests pin the client copy byte-for-byte. */

const WIPE = "\x1b[3J";

describe("which engines wipe on repaint comes from the roster (#853 P4)", () => {
  test("codex and kimi strip; every other engine passes through", () => {
    // From the manifests' `terminal.repaint` via the generated roster fixture — not a client set.
    const all = ["claude", "opencode", "codex", "gemini", "antigravity", "kimi", "shell"];
    expect(all.filter(wipesOnRepaint)).toEqual(["codex", "kimi"]);
  });

  test("an unknown engine, or no roster yet, passes through untouched", () => {
    const s = createScrollEraseStripper();
    const bytes = new TextEncoder().encode(`${WIPE}x`);
    expect(s.strip("zeta", true, bytes)).toEqual(bytes);
    resetRoster();
    expect(s.strip("codex", true, bytes)).toEqual(bytes);
  });
});

describe("strip — full wipe inside one chunk", () => {
  test.each(["codex", "kimi"])("%s: the agent's clear-scrollback is dropped", (engine) => {
    const s = createScrollEraseStripper();
    const boot = new TextEncoder().encode(
      `\x1b[?2026h\x1b[2J\x1b[H${WIPE}transcript\x1b[?2026l`,
    );
    const out = new TextDecoder().decode(s.strip(engine, true, boot));
    expect(out).toBe("\x1b[?2026h\x1b[2J\x1b[Htranscript\x1b[?2026l");
  });

  test.each(["codex", "kimi"])("%s: multiple wipes in one chunk all go", (engine) => {
    const s = createScrollEraseStripper();
    const bytes = new TextEncoder().encode(`a${WIPE}b${WIPE}c`);
    expect(new TextDecoder().decode(s.strip(engine, true, bytes))).toBe("abc");
  });
});

describe("strip — non-member engines and the attach replay", () => {
  test("other engines pass through byte-for-byte", () => {
    const s = createScrollEraseStripper();
    const bytes = new TextEncoder().encode(`keep${WIPE}this`);
    expect(s.strip("claude", true, bytes)).toBe(bytes);
    expect(s.strip("opencode", true, bytes)).toBe(bytes);
  });

  test("the attach replay (live=false) keeps its wipes and drops a stale carry", () => {
    const s = createScrollEraseStripper();
    // A partial wipe left over from a pre-boundary chunk…
    const partial = new TextEncoder().encode(`x${WIPE.slice(0, 3)}`);
    expect(new TextDecoder().decode(s.strip("kimi", true, partial))).toBe("x");
    // …must not eat into the replay.
    const replay = new TextEncoder().encode(`y${WIPE}z`);
    expect(new TextDecoder().decode(s.strip("kimi", false, replay))).toBe(`y${WIPE}z`);
    // …and the post-seq live stream is stripped again from a clean carry.
    const live = new TextEncoder().encode(`a${WIPE}b`);
    expect(new TextDecoder().decode(s.strip("kimi", true, live))).toBe("ab");
  });
});

describe("strip — across-chunk carry", () => {
  test.each(["codex", "kimi"])("%s: the wipe is dropped when split at EVERY offset", (engine) => {
    // Only the ESC[3J is ever dropped; the authored ESC[2J ESC[H clear passes through. The
    // sweep covers EVERY byte offset of the whole fixture, so the ESC[3J itself is cut at all
    // four of its internal boundaries (the review found the first cut only ever landed in the
    // ESC[2J; Hermes #1043).
    const whole = new TextEncoder().encode(`\x1b[2J\x1b[H${WIPE}frame`);
    const expected = "\x1b[2J\x1b[Hframe";
    expect(Array.from(whole).join(",")).toContain("27,91,51,74"); // the wipe is present
    for (let cut = 0; cut <= whole.length; cut++) {
      const s = createScrollEraseStripper();
      const first = s.strip(engine, true, whole.slice(0, cut));
      const second = s.strip(engine, true, whole.slice(cut));
      const merged = new Uint8Array(first.length + second.length);
      merged.set(first, 0);
      merged.set(second, first.length);
      expect(new TextDecoder().decode(merged), `split at ${cut}`).toBe(expected);
    }
  });

  test("a partial carry followed by nonmatching bytes is emitted verbatim", () => {
    const s = createScrollEraseStripper();
    expect(new TextDecoder().decode(s.strip("kimi", true, new TextEncoder().encode("x\x1b[3")))).toBe("x");
    expect(new TextDecoder().decode(s.strip("kimi", true, new TextEncoder().encode("Xjunk")))).toBe("\x1b[3Xjunk");
  });

  test("reset() drops a pending partial carry at a stream boundary", () => {
    const s = createScrollEraseStripper();
    expect(new TextDecoder().decode(s.strip("kimi", true, new TextEncoder().encode("\x1b[3")))).toBe("");
    s.reset();
    expect(new TextDecoder().decode(s.strip("kimi", true, new TextEncoder().encode("3Jtail")))).toBe("3Jtail");
  });

  test("the unchanged fast path returns the SAME array (no copy on the hot path)", () => {
    const s = createScrollEraseStripper();
    const bytes = new TextEncoder().encode("plain stream bytes");
    expect(s.strip("kimi", true, bytes)).toBe(bytes);
  });
});
