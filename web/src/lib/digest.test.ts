/** The objective digest is a CROSS-LANGUAGE contract, and it is driven by one shared fixture.
 *
 *  The client sends it, the server recomputes it, and DISPATCH refuses when they differ — so a
 *  disagreement is not a wrong answer, it is a mission that cannot be started. Two hand-written
 *  encodings are exactly the thing that drifts, so `tests/fixtures/objectives_digest_cases.json`
 *  carries the rows AND the digest `missions.objectives_digest` produced for them, and both
 *  suites assert against it.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "vitest";

import { objectivesDigestInput, sha256Hex } from "./digest";

// Resolved from the vitest root (web/), not import.meta.url: under vitest's transform that is
// not a file: URL and `new URL(...)` throws before a single case runs.
const FIXTURE = resolve(
  process.cwd(),
  "../tests/fixtures/objectives_digest_cases.json",
);
const CASES = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  cases: {
    why: string;
    rows: { key: string; title?: string | null; gate?: boolean }[];
    digest: string;
  }[];
};

/** The two C0 characters the OLD encoding used as separators. Written as escapes rather than
 *  pasted, so the file stays readable and greppable. */
const US = "\u001f";
const RS = "\u001e";

describe("objectivesDigest agrees with the server", () => {
  for (const c of CASES.cases) {
    test(c.why, async () => {
      expect(await sha256Hex(objectivesDigestInput(c.rows))).toBe(c.digest);
    });
  }

  test("the delimiter collision is gone in both directions", async () => {
    // #904 review 10, finding 3, stated as a property rather than as two fixture rows that
    // happen to differ: a title that SPELLS the old field and row separators must not serialize
    // to the same thing as a genuinely different checklist.
    // SOLVED, not guessed: under the old encoding these two produce the same hashed string. A
    // pair that merely differs would pass against the defect and prove nothing.
    const one = [{ key: "a", title: `x${US}1${RS}z${US}z`, gate: true }];
    const two = [
      { key: "a", title: "x", gate: true },
      { key: "z", title: "z", gate: true },
    ];
    expect(objectivesDigestInput(one)).not.toBe(objectivesDigestInput(two));
    expect(await sha256Hex(objectivesDigestInput(one))).not.toBe(
      await sha256Hex(objectivesDigestInput(two)),
    );
  });

  test("a REORDER of the same set is the same approval", () => {
    const a = [
      { key: "aa", title: "A PR is open", gate: false },
      { key: "zz", title: "Merged", gate: true },
    ];
    expect(objectivesDigestInput([...a].reverse())).toBe(
      objectivesDigestInput(a),
    );
  });
});
