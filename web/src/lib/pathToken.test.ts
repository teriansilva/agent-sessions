import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { pathToken, relativise, spliceToken } from "./pathToken";

const CWD = "/home/u/proj";

describe("relativise", () => {
  it("relativises a path inside the cwd", () => {
    expect(relativise("/home/u/proj/src/a.py", CWD)).toBe("src/a.py");
  });

  it("leaves a path outside the cwd absolute", () => {
    expect(relativise("/etc/hosts", CWD)).toBe("/etc/hosts");
  });

  it("does not treat a sibling with a shared prefix as inside", () => {
    // A plain `startsWith` yields "2" here — a different file entirely, named confidently.
    expect(relativise("/home/u/proj2/a.py", CWD)).toBe("/home/u/proj2/a.py");
  });
});

describe("pathToken", () => {
  it("leaves an inert path bare", () => {
    expect(pathToken("/home/u/proj/src/a.py", CWD)).toBe("src/a.py");
  });

  it("quotes a path containing a space", () => {
    expect(pathToken("/home/u/proj/my file.py", CWD)).toBe("'my file.py'");
  });

  it("escapes an embedded single quote", () => {
    expect(pathToken("/home/u/proj/it's.py", CWD)).toBe(`'it'\\''s.py'`);
  });

  it("re-anchors a leading dash instead of only quoting it", () => {
    // The whole point of finding 1: `'-l'` is still parsed as an option.
    expect(pathToken("/home/u/proj/-l", CWD)).toBe("./-l");
  });

  it("re-anchors AND quotes when a dash path also needs quoting", () => {
    expect(pathToken("/home/u/proj/-l x", CWD)).toBe("'./-l x'");
  });

  it("never re-anchors an absolute path", () => {
    expect(pathToken("/etc/hosts", CWD)).toBe("/etc/hosts");
  });
});

/** The assertion that string comparison cannot make.
 *
 *  Every hostile name below is written to a real directory, turned into a token, and then handed
 *  to a real `sh -c` as an argument to `cat`. The test passes only if the shell read back the
 *  exact bytes the file holds — i.e. the token addressed the intended file and executed nothing
 *  else. A snapshot test would have happily locked in `'-l'`, which does not work. */
describe("hostile filenames survive a real shell", () => {
  let dir: string;
  // A slash-free name so it can live INSIDE a filename; the canary lands in the test dir.
  const CANARY = "canary-fired";

  const NAMES = [
    "plain.txt",
    "with space.txt",
    "it's.txt",
    '"quoted".txt',
    "semi;colon.txt",
    "amp&&and.txt",
    "pipe|d.txt",
    "dollar$var.txt",
    "sub$(id).txt",
    "back`tick`.txt",
    "star*.txt",
    "quest?.txt",
    "brack[et].txt",
    "paren(s).txt",
    "new\nline.txt",
    "tab\there.txt",
    "-l",
    "--version",
    "-rf .",
    "; rm -rf ~",
    "$(touch " + CANARY + ")",
    "~tilde.txt",
    "#hash.txt",
    "emoji✓.txt",
  ];

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "pathtoken-"));
    for (const [i, name] of NAMES.entries()) {
      writeFileSync(join(dir, name), `content-${i}`);
    }
  });

  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it.each(NAMES.map((n, i) => [JSON.stringify(n), n, i] as const))(
    "%s addresses its own file and nothing else",
    (_label, name, i) => {
      const token = pathToken(join(dir, name), dir);
      // `cd` into the directory so the token is exercised as the RELATIVE form it will normally
      // take — which is the form the leading-dash rule exists for.
      //
      // NOTE the deliberate absence of `--`. The first version of this test used `cat -- ${token}`
      // and passed with the dash rule REMOVED, because the terminator makes a leading dash safe on
      // its own — the test proved the terminator worked, not the code. The user typing into the
      // compose box will not add `--`, so neither does this.
      const out = execFileSync(
        "sh",
        ["-c", `cd ${JSON.stringify(dir)} && cat ${token}`],
        {
          encoding: "utf8",
        },
      );
      expect(out).toBe(`content-${i}`);
    },
  );

  it("does not let a command-substitution name execute", () => {
    const token = pathToken(join(dir, "$(touch " + CANARY + ")"), dir);
    execFileSync("sh", ["-c", `cd ${JSON.stringify(dir)} && cat ${token}`], {
      encoding: "utf8",
    });
    // If the token had been interpolated bare, the shell would have run `touch` first and the
    // canary would exist. Its ABSENCE is the assertion.
    expect(existsSync(join(dir, CANARY))).toBe(false);
  });
});

describe("spliceToken", () => {
  it("appends to an empty draft without a leading space", () => {
    expect(spliceToken("", 0, 0, "a.py")).toEqual({ text: "a.py", caret: 4 });
  });

  it("adds one separator when inserting at the end of a word", () => {
    expect(spliceToken("look at", 7, 7, "a.py")).toEqual({
      text: "look at a.py",
      caret: 12,
    });
  });

  it("keeps exactly one space on each side mid-sentence", () => {
    const r = spliceToken("look at and tell me", 8, 8, "a.py");
    expect(r.text).toBe("look at a.py and tell me");
    expect(r.text).not.toContain("  ");
  });

  it("does not double an existing separator", () => {
    expect(spliceToken("look at ", 8, 8, "a.py").text).toBe("look at a.py");
  });

  it("replaces the selection rather than inserting beside it", () => {
    expect(spliceToken("look at XXX now", 8, 11, "a.py").text).toBe(
      "look at a.py now",
    );
  });

  it("puts the caret after the token, not after the trailing space", () => {
    const r = spliceToken("look at and tell", 8, 8, "a.py");
    expect(r.text.slice(0, r.caret)).toBe("look at a.py");
  });
});
