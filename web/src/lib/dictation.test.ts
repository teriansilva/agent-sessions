import { expect, test } from "vitest";
import { assembleSpoken, isSpaceDelimitedLang, SNAPSHOT_BURST_MS, type SpokenSegment } from "./dictation";

// Entry shorthands. `fin` is a final-born entry — it materialized already finalized, the shape of a
// stacking engine's restatements AND of a compliant engine's finals when no interim preceded them.
// `grown` is an entry that lived as an interim first — a compliant engine narrating an utterance.
// eventSeq defaults to a fresh event per entry (the observed stacker's one-appended-entry-per-event
// drip); pass it explicitly to model several entries first exposed by ONE onresult event.
// A `fin("")` entry is the stacker's fingerprint (#711 finding 3): a finalized empty result, which
// no compliant engine produces — it is what arms collapsing at all.
let seq = 0;
const fin = (text: string, atMs: number, eventSeq = ++seq): SpokenSegment => ({
  text,
  atMs,
  eventSeq,
  isFinal: true,
  finalBorn: true,
});
const grown = (text: string, atMs: number, eventSeq = ++seq): SpokenSegment => ({
  text,
  atMs,
  eventSeq,
  isFinal: true,
  finalBorn: false,
});

// --- the gate (#711 round 6): no positive stacker identification ⇒ no collapse, ever -------------

test("finals from an engine never identified as stacking are NEVER collapsed (#711 round 6)", () => {
  // Every per-pair signal fires here — born final, later event, inside the burst window, textual
  // restatement — and none of it matters: callback cadence can be compressed by a main-thread
  // stall or service buffering, so without the engine's own fingerprint nothing may be deleted.
  expect(assembleSpoken([fin("yes", 1000), fin("yes", 1499)], true)).toBe("yes yes");
  expect(assembleSpoken([fin("go", 1000), fin("go now", 1499)], true)).toBe("go go now");
  expect(assembleSpoken([fin("cat", 1000), fin("bat this", 1300)], true)).toBe("cat bat this");
});

test("a finalized empty entry is the fingerprint that arms collapsing (#711 finding 3)", () => {
  // Identical input either side of the gate: with the fingerprint the burst-cadence chain is a
  // restatement and collapses; without it, it is unknown speech and survives.
  expect(assembleSpoken([fin("", 0), fin("this", 300), fin("this is a test", 550)], true)).toBe(
    "this is a test",
  );
  expect(assembleSpoken([fin("this", 300), fin("this is a test", 550)], true)).toBe(
    "this this is a test",
  );
});

// --- the reported bug: the real capture ----------------------------------------------------------

test("replays the captured Android Chrome 150 session at its real timestamps (#711)", () => {
  // 8 events over 2.2s, every entry born final, two finalized empty strings, snapshots landing
  // 161–487ms apart. On v0.13.0 this typed "this this is this is this is this is a this is a test".
  const captured = [
    fin("", 1931),
    fin("", 2192),
    fin("this", 2519),
    fin("this is", 2733),
    fin("this is", 3059),
    fin("this is", 3459),
    fin("this is a", 3620),
    fin("this is a test", 4107),
  ];
  expect(assembleSpoken(captured, true)).toBe("this is a test");
});

// --- on the identified stacker, real speech still survives every per-pair bound ------------------

test("a repeated utterance spoken twice on the identified stacker survives (#711)", () => {
  expect(assembleSpoken([fin("", 0), fin("yes", 1000), fin("yes", 3000)], true)).toBe("yes yes");
});

test("three genuine prefix-extending utterances on the identified stacker all survive (#711 round 4)", () => {
  // The counterexample against a chain-length floor: three REAL utterances that happen to extend
  // one another, each landing seconds after the previous (speech + endpointing) — none deleted.
  expect(
    assembleSpoken([fin("", 0), fin("go", 1000), fin("go now", 3200), fin("go now please", 5600)], true),
  ).toBe("go go now go now please");
});

test("saying the same phrase three times slowly keeps all three, even on the stacker", () => {
  expect(
    assembleSpoken(
      [fin("", 0), fin("hello there", 1000), fin("hello there", 3000), fin("hello there", 5000)],
      true,
    ),
  ).toBe("hello there hello there hello there");
});

test("the burst window is a strict boundary on the identified stacker (#711 round 5)", () => {
  expect(
    assembleSpoken([fin("", 0), fin("hey", 1000), fin("hey there", 1000 + SNAPSHOT_BURST_MS - 1)], true),
  ).toBe("hey there");
  expect(
    assembleSpoken([fin("", 0), fin("hey", 1000), fin("hey there", 1000 + SNAPSHOT_BURST_MS)], true),
  ).toBe("hey hey there");
});

test("entries first exposed by the SAME event are preserved even on the identified stacker (#711 round 5)", () => {
  // A service may buffer and deliver two genuine finals in one onresult; they share a single
  // sampled timestamp, which is no evidence of snapshot cadence — both must survive.
  expect(assembleSpoken([fin("", 500), fin("go", 1000, 7), fin("go now", 1000, 7)], true)).toBe(
    "go go now",
  );
});

test("an entry that lived as an interim never supersedes, however fast it arrives (#711)", () => {
  // A compliant narration (interim first) is never a restatement — even on the identified stacker.
  expect(assembleSpoken([fin("", 0), fin("go", 300), grown("go now", 500)], true)).toBe("go go now");
});

test("one interim does not exempt later stacked snapshots (#711 follow-up)", () => {
  // The latch counterexample: an interim-tracked entry followed by pre-finalized restatements at
  // cadence must still collapse — the decision is per entry pair, never a session classification.
  expect(
    assembleSpoken([fin("", 0), grown("this", 300), fin("this is", 600), fin("this is a test", 850)], true),
  ).toBe("this is a test");
});

// --- the textual leg on the identified stacker ---------------------------------------------------

test("an all-final lexical revision chain collapses to the corrected form (#711 round 4)", () => {
  expect(
    assembleSpoken(
      [fin("", 0), fin("recognise", 300), fin("recognize", 600), fin("recognize this", 900)],
      true,
    ),
  ).toBe("recognize this");
  expect(assembleSpoken([fin("", 0), fin("dont", 300), fin("don't stop", 580)], true)).toBe("don't stop");
});

test("a revision-shaped pair spoken seconds apart is two real utterances", () => {
  expect(assembleSpoken([fin("", 0), fin("recognise", 1000), fin("recognize", 3500)], true)).toBe(
    "recognise recognize",
  );
});

test("short words never alias each other as revisions — 'no' is not a draft of 'go'", () => {
  expect(assembleSpoken([fin("", 0), fin("no", 300), fin("go home", 600)], true)).toBe("no go home");
});

test("a snapshot may extend its trailing word mid-recognition ('and' → 'android studio')", () => {
  expect(assembleSpoken([fin("", 0), fin("and", 300), fin("android studio", 600)], true)).toBe(
    "android studio",
  );
  // …but the same pair spoken as two utterances (seconds apart) keeps both.
  expect(assembleSpoken([fin("", 0), fin("and", 300), fin("android studio", 2300)], true)).toBe(
    "and android studio",
  );
});

test("only the trailing word may be revised — interior words must match exactly", () => {
  expect(assembleSpoken([fin("", 0), fin("and when", 300), fin("android when you", 600)], true)).toBe(
    "and when android when you",
  );
});

test("supersession survives re-punctuation and re-capitalization (#711)", () => {
  expect(
    assembleSpoken(
      [
        fin("", 0),
        fin("hey claude", 300),
        fin("Hey, Claude — can you", 600),
        fin("Hey, Claude, can you deploy?", 900),
      ],
      true,
    ),
  ).toBe("Hey, Claude, can you deploy?");
});

test("a stacked chain followed by genuinely different words keeps both sides", () => {
  // The last entry lands in-burst but restates nothing — the textual leg alone protects it.
  expect(
    assembleSpoken(
      [fin("", 0), fin("ship", 300), fin("ship it", 600), fin("ship it now", 900), fin("then tell me", 1200)],
      true,
    ),
  ).toBe("ship it now then tell me");
});

// --- punctuation-only segments (#711 finding 2) --------------------------------------------------

test("punctuation-only finalized segments are preserved, not dropped (#711 finding 2)", () => {
  expect(assembleSpoken([fin("", 0), fin("hello", 300), fin(".", 500)], true)).toBe("hello .");
  expect(assembleSpoken([fin("", 0), fin("use option", 300), fin("--", 500)], true)).toBe(
    "use option --",
  );
});

test("a punctuation segment breaks a chain and is never swept up by a later prefix", () => {
  expect(
    assembleSpoken([fin("", 0), fin("use", 300), fin("--", 500), fin("use option now", 700)], true),
  ).toBe("use -- use option now");
});

// --- non-space-delimited languages (#711 finding 3) ----------------------------------------------

test("space-delimited languages are recognized; scriptless ones are not", () => {
  for (const l of ["en-US", "en", "de-DE", "fr", "es-419", "ru", undefined]) {
    expect(isSpaceDelimitedLang(l)).toBe(true);
  }
  for (const l of ["zh-CN", "zh", "ja-JP", "ja", "th-TH", "km", "my", "lo", "bo"]) {
    expect(isSpaceDelimitedLang(l)).toBe(false);
  }
});

test("a CJK snapshot chain on the identified stacker collapses by codepoint prefix (#711)", () => {
  expect(
    assembleSpoken([fin("", 0), fin("你", 300), fin("你好", 600), fin("你好世界", 900)], false),
  ).toBe("你好世界");
});

test("CJK utterances spoken separately are preserved", () => {
  expect(assembleSpoken([fin("", 0), fin("你好", 1000), fin("你好世界", 3500)], false)).toBe(
    "你好 你好世界",
  );
});

// --- shared hygiene ------------------------------------------------------------------------------

test("empty and whitespace-only entries carry no speech and are dropped from output", () => {
  expect(assembleSpoken([fin("", 0), fin("   ", 100), fin("okay", 200)], true)).toBe("okay");
  expect(assembleSpoken([], true)).toBe("");
  expect(assembleSpoken([], false)).toBe("");
});

test("interior whitespace is normalized in every mode", () => {
  expect(assembleSpoken([fin("  deploy   the    build  ", 0)], true)).toBe("deploy the build");
  expect(assembleSpoken([fin("  deploy   the    build  ", 0)], false)).toBe("deploy the build");
});
