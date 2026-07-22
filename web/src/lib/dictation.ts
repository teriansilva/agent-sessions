/** Transcript assembly for push-to-talk dictation (#483).
 *
 *  Lives here rather than in Compose.tsx because it is pure engine-quirk logic: the browser speech
 *  engines disagree about how a phrase in progress is represented, and untangling that is worth
 *  testing directly rather than only through a rendered textarea. */

/** One entry of the recognizer's cumulative results list, as tracked by the caller.
 *
 *  `atMs` is when the entry's transcript last changed (performance.now() domain): the single
 *  appearance time for a snapshot-stacker's entries (#711), the latest revision for a live interim.
 *  `eventSeq` is which `onresult` callback last changed it — entries first exposed by the SAME
 *  event share a sampled timestamp that says nothing about their individual arrival, so they carry
 *  no per-entry cadence evidence against each other (a service may buffer and deliver several
 *  genuine finals in one event). `isFinal` is the entry's current flag. `finalBorn` is whether it
 *  was ALREADY `isFinal` the first time it existed. A compliant engine narrates an utterance
 *  through interim entries and finalizes them in place, so an entry that materializes pre-finalized
 *  never carried a live phrase — that is how a stacking engine restates speech it already
 *  reported. */
export type SpokenSegment = {
  text: string;
  atMs: number;
  eventSeq: number;
  isFinal: boolean;
  finalBorn: boolean;
};

/** Positive identification of the snapshot-stacking mode (#711 finding 3): a FINALIZED entry with
 *  an empty transcript. No compliant engine finalizes an empty result — the spec has nothing for
 *  such an entry to mean — while the captured Android Chrome 150 stream finalizes two of them
 *  before restating the utterance. This is the gate that arms collapsing at all: without it every
 *  finalized entry is preserved verbatim, however the entries are shaped or timed, because
 *  callback-delivery cadence alone can never prove one final supersedes another (events can queue
 *  behind a main-thread stall or arrive batched from the service). A stacker that never finalizes
 *  an empty entry therefore stays duplicated — the recoverable failure — never collapsed. */
const identifiesStacker = (segments: SpokenSegment[]): boolean =>
  segments.some((s) => s.isFinal && s.text.trim() === "");

/** How close together two entries must land (strictly) for the later to be read as a restatement
 *  of the earlier (#711). The device capture behind #711 shows snapshots of one utterance arriving
 *  161–487 ms apart — the engine's interim cadence — so cadence evidence holds strictly below
 *  500 ms and no observed stacker gap is lost. A genuinely separate next utterance cannot be
 *  PRODUCED that fast (new audible speech plus the endpointer's trailing silence), and while
 *  delivery jitter can compress observed gaps of real finals, keeping the window at the observed
 *  cadence ceiling leaves such a compressed pair outside it. A stacker pausing longer than this
 *  between snapshots degrades to a duplicate — the recoverable failure — never to deleted
 *  speech. */
export const SNAPSHOT_BURST_MS = 500;

/** Words of a transcript segment, normalized for comparison: case-folded, punctuation stripped
 *  (apostrophes kept). Engines re-punctuate and re-capitalize a phrase between snapshots
 *  ("and when" → "And, when"), so a raw string compare would miss that one segment supersedes
 *  another. Returns `[]` for a segment with no word characters (pure punctuation) — such a segment
 *  never takes part in supersession in either direction, and is preserved verbatim. */
const spokenWords = (s: string): string[] =>
  s
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s']/gu, " ")
    .split(/\s+/)
    .filter(Boolean);

/** Levenshtein distance ≤ 1: equal, or one insert / delete / substitute apart. */
const within1Edit = (a: string, b: string): boolean => {
  if (a === b) return true;
  const [s, l] = a.length <= b.length ? [a, b] : [b, a];
  if (l.length - s.length > 1) return false;
  let i = 0;
  let j = 0;
  let edits = 0;
  while (i < s.length && j < l.length) {
    if (s[i] === l[j]) {
      i++;
      j++;
      continue;
    }
    if (++edits > 1) return false;
    if (s.length === l.length) i++; // substitution consumes a char of both
    j++; // insertion consumes only the longer side
  }
  return edits + (s.length - i) + (l.length - j) <= 1;
};

/** Whether word `b` reads as a revision of word `a` between snapshots: the same word, a longer
 *  word begun by it (a snapshot freezes mid-word: "recogni" → "recognize"), or a spelling
 *  correction one edit away ("recognise" → "recognize", "dont" → "don't"). The edit branch
 *  requires ≥3 chars on both sides so short words can't alias each other ("no" is never a draft
 *  of "go"). */
const revisesToken = (a: string, b: string): boolean =>
  a === b || b.startsWith(a) || (a.length >= 3 && b.length >= 3 && within1Edit(a, b));

/** Whether transcript `b` restates transcript `a` — the textual leg of the supersession evidence.
 *
 *  Space-delimited languages compare whole normalized words: every word of `a` must reappear in
 *  place in `b` except the last, which may be revised (`revisesToken`) — the trailing word is the
 *  one still forming when a snapshot freezes, so it is the only one the next snapshot may rewrite.
 *  Without word boundaries (CJK etc. — see `isSpaceDelimitedLang`) the comparison is a plain
 *  codepoint prefix over the word characters. */
const restates = (a: string, b: string, spaceDelimited: boolean): boolean => {
  if (spaceDelimited) {
    const wa = spokenWords(a);
    const wb = spokenWords(b);
    if (!wa.length || !wb.length || wa.length > wb.length) return false;
    for (let i = 0; i < wa.length - 1; i++) if (wa[i] !== wb[i]) return false;
    return revisesToken(wa[wa.length - 1], wb[wa.length - 1]);
  }
  const na = a.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  const nb = b.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  return na.length > 0 && nb.length > 0 && nb.startsWith(na);
};

/** Whether entry `b` supersedes entry `a` — the engine restated `a` rather than the user saying
 *  something new (#711). Only consulted once `identifiesStacker` has armed collapsing; on the
 *  identified engine, four per-pair signals bound what may collapse, ALL required:
 *
 *    • `b` was born final — it never carried a live interim phrase (see `SpokenSegment`);
 *    • `b` arrived in a LATER event than `a`'s last change — entries first exposed together share
 *      one sampled timestamp and have no individual cadence, and a service may buffer several
 *      genuine finals into one event, so same-event neighbours are always preserved. The observed
 *      stacker appends exactly one entry per event, so its chains are unaffected;
 *    • `b` landed strictly within `SNAPSHOT_BURST_MS` of `a`'s last change — the identified
 *      stacker's restatement cadence, so that real repeated speech spoken on the broken device
 *      ("yes", a pause, "yes") is still typed twice;
 *    • `b` textually restates `a` (`restates`). */
const supersedes = (a: SpokenSegment, b: SpokenSegment, spaceDelimited: boolean): boolean =>
  b.finalBorn &&
  b.eventSeq > a.eventSeq &&
  b.atMs - a.atMs < SNAPSHOT_BURST_MS &&
  restates(a.text, b.text, spaceDelimited);

/** Assemble what the user actually said from the recognizer's entries (#648, #711).
 *
 *  Empty / whitespace-only entries carry no speech and are dropped from the output — but a
 *  FINALIZED empty entry is first read as the stacker's fingerprint (`identifiesStacker`), the
 *  positive engine identification that arms collapsing at all. Once armed, adjacent entries
 *  collapse pairwise when `supersedes` holds — the captured 8-event chain reduces to its final
 *  form link by link. Unarmed, or for any pair failing any leg of the evidence, entries are
 *  concatenated verbatim: finals from an engine not positively identified as stacking are never
 *  deleted, whatever their shape or timing. */
export const assembleSpoken = (segments: SpokenSegment[], spaceDelimited: boolean): string => {
  const stacking = identifiesStacker(segments);
  const kept: SpokenSegment[] = [];
  for (const raw of segments) {
    const text = raw.text.replace(/\s+/g, " ").trim();
    if (!text) continue;
    const seg = { ...raw, text };
    const prev = kept[kept.length - 1];
    if (stacking && prev && supersedes(prev, seg, spaceDelimited)) kept[kept.length - 1] = seg;
    else kept.push(seg);
  }
  return kept.map((s) => s.text).join(" ");
};

/** Whether a recognizer language is written with spaces between words — selects how `restates`
 *  compares transcripts: whole-word comparison where spaces delimit words, plain codepoint prefix
 *  where they don't. A conservative allowlist of scriptless / space-optional languages returns
 *  false (Chinese, Japanese, Thai, Lao, Khmer, Burmese, Tibetan); everything else — incl. the
 *  reported `en-US` — compares by words. */
export const isSpaceDelimitedLang = (lang: string | undefined): boolean => {
  const base = (lang || "en").toLowerCase().split("-")[0];
  return !["zh", "ja", "th", "lo", "km", "my", "bo"].includes(base);
};
