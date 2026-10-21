import { describe, expect, it } from "vitest";
import {
  MAX_BATCH_BYTES,
  MAX_BATCH_FILES,
  MAX_FILE_BYTES,
  type FsEntryLike,
  type PlannedFile,
  filesFromDataTransfer,
  humanSize,
  planUpload,
  readAllEntries,
  relpathReason,
  walkEntry,
} from "./uploadPlan";

const f = (relpath: string, size = 10): PlannedFile => ({
  file: new File(["x"], relpath.split("/").pop() ?? "f"),
  relpath,
  size,
});

describe("relpath validation (#807)", () => {
  it.each([
    ["../escape.txt", /traversal/],
    ["a/../../escape.txt", /traversal/],
    ["/abs.txt", /absolute/],
    ["a//b.txt", /traversal/],
    ["./x.txt", /traversal/],
    [".git/hooks/pre-commit", /git metadata/],
    ["a/.git/config", /git metadata/],
  ])("refuses %s", (bad, why) => {
    expect(relpathReason(bad)).toMatch(why);
  });

  it("accepts ordinary and awkward-but-legitimate names", () => {
    for (const ok of [
      "notes.md",
      "src/lib/util.ts",
      "a file with spaces.txt",
      "ünïcodé.txt",
      "quo'te.txt",
    ]) {
      expect(relpathReason(ok)).toBeNull();
    }
  });

  it("refuses a name carrying a control character", () => {
    // Built from a codepoint so the test file itself never carries a raw control byte.
    const nl = String.fromCharCode(10);
    expect(relpathReason(`a${nl}b.txt`)).toMatch(/control/);
    expect(relpathReason(`a${String.fromCharCode(0)}b.txt`)).toMatch(/control/);
  });
});

describe("planning (#807)", () => {
  it("excludes one oversized file without losing the other thirty-nine", () => {
    const files = [
      ...Array.from({ length: 39 }, (_, i) => f(`f${i}.txt`, 1000)),
      f("huge.bin", MAX_FILE_BYTES + 1),
    ];
    const plan = planUpload(files);
    expect(plan.files).toHaveLength(39);
    expect(plan.rejected).toHaveLength(1);
    expect(plan.rejected[0].reason).toMatch(/TOO LARGE/);
    // The batch still goes: one red row, not a lost upload.
    expect(plan.refusal).toBeNull();
  });

  it("refuses the whole batch when there are too many files, naming the limit", () => {
    const plan = planUpload(Array.from({ length: MAX_BATCH_FILES + 1 }, (_, i) => f(`f${i}`)));
    expect(plan.refusal).toContain(String(MAX_BATCH_FILES));
    expect(plan.refusal).toMatch(/at most/);
  });

  it("refuses the whole batch when it is over the byte budget", () => {
    const each = 20 * 1024 * 1024;
    const n = Math.ceil(MAX_BATCH_BYTES / each) + 1;
    const plan = planUpload(Array.from({ length: n }, (_, i) => f(`f${i}`, each)));
    expect(plan.refusal).toMatch(/MB/);
  });

  it("counts only the files it kept", () => {
    const plan = planUpload([f("a", 100), f("big", MAX_FILE_BYTES + 1), f("b", 50)]);
    expect(plan.bytes).toBe(150);
  });

  it("keeps a rejected file's own reason on its own row", () => {
    const plan = planUpload([f("../evil", 10), f("ok.txt", 10)]);
    expect(plan.files.map((p) => p.relpath)).toEqual(["ok.txt"]);
    expect(plan.rejected[0]).toEqual({ relpath: "../evil", reason: "traversal segment in the path" });
  });
});

describe("the readEntries pagination gotcha (#807)", () => {
  it("keeps calling until the reader returns empty — 100 is not the whole folder", async () => {
    // Chromium hands back AT MOST 100 entries per call and signals done with an empty array. A
    // single call silently uploads the first 100 children and reports success, which is the
    // quiet truncation this panel's honesty rule exists to prevent.
    const total = 250;
    let served = 0;
    const reader = {
      readEntries(cb: (e: FsEntryLike[]) => void) {
        const n = Math.min(100, total - served);
        const batch = Array.from({ length: n }, (_, i) => ({
          isFile: true,
          isDirectory: false,
          name: `f${served + i}`,
        })) as FsEntryLike[];
        served += n;
        cb(batch);
      },
    };
    expect(await readAllEntries(reader)).toHaveLength(total);
  });

  it("propagates a reader error rather than resolving with a partial folder", async () => {
    const reader = {
      readEntries(_cb: (e: FsEntryLike[]) => void, err?: (e: unknown) => void) {
        err?.(new Error("nope"));
      },
    };
    await expect(readAllEntries(reader)).rejects.toThrow("nope");
  });
});

describe("folder walk (#807)", () => {
  const fileEntry = (name: string): FsEntryLike => ({
    isFile: true,
    isDirectory: false,
    name,
    file: (cb) => cb(new File(["xy"], name)),
  });
  const dirEntry = (name: string, children: FsEntryLike[]): FsEntryLike => ({
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let done = false;
      return {
        readEntries(cb: (e: FsEntryLike[]) => void) {
          // The flag flips BEFORE the callback. These readers are synchronous, so `cb` re-enters
          // `readEntries` immediately — setting `done` afterwards would never be reached and the
          // same children would be served forever (it blew the stack, exactly as a real reader
          // that never returns empty would).
          const batch = done ? [] : children;
          done = true;
          cb(batch);
        },
      };
    },
  });

  it("preserves structure, so src/lib/util.ts survives the drop", async () => {
    const tree = dirEntry("src", [dirEntry("lib", [fileEntry("util.ts")]), fileEntry("main.ts")]);
    const out = await walkEntry(tree);
    expect(out.map((p) => p.relpath).sort()).toEqual(["src/lib/util.ts", "src/main.ts"]);
  });

  it("stops just past the ceiling instead of enumerating a mis-dropped node_modules", async () => {
    // Bounded, but deliberately ONE past the limit rather than exactly at it: a plan of exactly
    // MAX is not `> MAX`, so stopping at the cap meant the refusal never fired and the first MAX
    // files uploaded in silence. See the "honest truncation" block below.
    const many = Array.from({ length: MAX_BATCH_FILES + 50 }, (_, i) => fileEntry(`f${i}`));
    const out = await walkEntry(dirEntry("node_modules", many));
    expect(out.length).toBeLessThanOrEqual(MAX_BATCH_FILES + 1);
    expect(out.length).toBeGreaterThan(MAX_BATCH_FILES);
  });

  it("reads a folder of more than 100 entries whole", async () => {
    // The walk's own end-to-end version of the pagination test above: a real fixture folder of
    // >100 children, which is what the issue names as the test for this.
    let served = 0;
    const total = 137;
    const big: FsEntryLike = {
      isFile: false,
      isDirectory: true,
      name: "big",
      createReader: () => ({
        readEntries(cb: (e: FsEntryLike[]) => void) {
          const n = Math.min(100, total - served);
          const batch = Array.from({ length: n }, (_, i) => fileEntry(`f${served + i}`));
          served += n;
          cb(batch);
        },
      }),
    };
    const out = await walkEntry(big);
    expect(out).toHaveLength(total);
  });
});

describe("sizes", () => {
  it("reads as a size, not a byte count", () => {
    expect(humanSize(512)).toBe("512 B");
    expect(humanSize(2048)).toBe("2 KB");
    expect(humanSize(41 * 1024 * 1024)).toBe("41 MB");
  });
});

describe("honest truncation at the batch ceiling (#827 review)", () => {
  const fileEntry = (name: string): FsEntryLike => ({
    isFile: true,
    isDirectory: false,
    name,
    file: (cb) => cb(new File(["xy"], name)),
  });
  const dirOf = (name: string, kids: FsEntryLike[]): FsEntryLike => ({
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let done = false;
      return {
        readEntries(cb: (e: FsEntryLike[]) => void) {
          const batch = done ? [] : kids;
          done = true;
          cb(batch);
        },
      };
    },
  });

  it("a folder of MAX+1 is REFUSED, not silently trimmed to MAX", async () => {
    // Stopping at exactly MAX produced a plan of exactly MAX, which is not `> MAX`, so the
    // refusal never fired and the first MAX files uploaded in silence. The walk now collects one
    // past the limit precisely so the refusal has something to see.
    const kids = Array.from({ length: MAX_BATCH_FILES + 1 }, (_, i) => fileEntry(`f${i}`));
    const out = await walkEntry(dirOf("big", kids));
    expect(out.length).toBeGreaterThan(MAX_BATCH_FILES);
    expect(planUpload(out).refusal).toContain(String(MAX_BATCH_FILES));
  });

  it("a folder of exactly MAX still uploads", async () => {
    const kids = Array.from({ length: MAX_BATCH_FILES }, (_, i) => fileEntry(`f${i}`));
    const plan = planUpload(await walkEntry(dirOf("exact", kids)));
    expect(plan.refusal).toBeNull();
    expect(plan.files).toHaveLength(MAX_BATCH_FILES);
  });

  it("a file the browser will not open becomes a REJECTED row, not a silent omission", async () => {
    const bad: FsEntryLike = {
      isFile: true,
      isDirectory: false,
      name: "locked.bin",
      file: (_cb, err) => err?.(new Error("NotReadableError")),
    };
    const out = await walkEntry(dirOf("mixed", [fileEntry("ok.txt"), bad]));
    const plan = planUpload(out);
    expect(plan.files.map((f) => f.relpath)).toEqual(["mixed/ok.txt"]);
    expect(plan.rejected).toEqual([
      { relpath: "mixed/locked.bin", reason: "UNREADABLE — the browser would not open it" },
    ]);
  });
});

describe("multi-root drops (#827 review)", () => {
  const fileEntry = (name: string): FsEntryLike => ({
    isFile: true,
    isDirectory: false,
    name,
    file: (cb) => cb(new File(["xy"], name)),
  });
  const dirOf = (name: string, kids: FsEntryLike[]): FsEntryLike => ({
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let done = false;
      return {
        readEntries(cb: (e: FsEntryLike[]) => void) {
          const batch = done ? [] : kids;
          done = true;
          cb(batch);
        },
      };
    },
  });

  it("one MAX-sized folder plus a second item is REFUSED, not silently trimmed", async () => {
    // `walkEntry` collects one past the ceiling, but the outer loop over top-level dropped items
    // stopped AT it — so 500 + 1 returned exactly 500, the `> MAX` refusal never fired, and the
    // second top-level item disappeared without a word.
    const big = dirOf("big", Array.from({ length: MAX_BATCH_FILES }, (_, i) => fileEntry(`f${i}`)));
    const dt = {
      items: [
        { kind: "file", webkitGetAsEntry: () => big },
        { kind: "file", webkitGetAsEntry: () => fileEntry("second.txt") },
      ],
    };
    const out = await filesFromDataTransfer(dt);
    expect(out.length).toBeGreaterThan(MAX_BATCH_FILES);
    expect(planUpload(out).refusal).toContain(String(MAX_BATCH_FILES));
  });
});
