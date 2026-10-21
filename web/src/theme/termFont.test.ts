import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, test } from "vitest";
import {
  coerceTermFontFamily,
  DEFAULT_TERM_FONT_FAMILY,
  hasStoredTermFontFamily,
  isTermFontFamily,
  presetForStack,
  readStoredTermFontFamily,
  storeTermFontFamily,
  TERM_FONT_FAMILY_MAX_LEN,
  TERM_FONT_PRESETS,
  TERM_FONT_STORAGE_KEY,
} from "./termFont";

// The SAME fixture tests/test_prefs.py reads (#866). Loaded through node:fs rather than a JSON
// import so nothing depends on the bundler resolving a path outside web/, and resolved from the
// vitest root rather than import.meta.url (which is not a file: URL under vitest's transform).
const FIXTURE = resolve(
  process.cwd(),
  "../tests/fixtures/term_font_family_cases.json",
);
const CASES = JSON.parse(readFileSync(FIXTURE, "utf8")) as {
  default: string;
  max_len: number;
  read: { in: unknown; out: string; why: string }[];
  write_accepted: string[];
  write_rejected: unknown[];
};

describe("termFont normalization", () => {
  test("the shared fixture describes THIS module's constants", () => {
    expect(CASES.default).toBe(DEFAULT_TERM_FONT_FAMILY);
    expect(CASES.max_len).toBe(TERM_FONT_FAMILY_MAX_LEN);
  });

  test("the shared read table", () => {
    for (const c of CASES.read) {
      expect(
        coerceTermFontFamily(c.in),
        `${JSON.stringify(c.in)}: ${c.why}`,
      ).toBe(c.out);
    }
  });

  test("the shared write table, via the canonical check", () => {
    // isTermFontFamily is stricter than the server's is_valid_term_font_family by exactly one
    // rule — it also demands the value be already trimmed, because that is what the store
    // writes. So untrimmed-but-otherwise-valid rows are asserted through coerce, not here.
    for (const v of CASES.write_accepted) {
      expect(isTermFontFamily(v.trim()), v).toBe(true);
    }
    for (const v of CASES.write_rejected) {
      expect(isTermFontFamily(v), JSON.stringify(v)).toBe(false);
    }
  });

  test("all-allowed-characters is NOT enough — the dead stacks are the point", () => {
    // Every one of these passes a charset check and then renders as nothing at all. They are
    // the reason `stackIsSane` exists rather than a single regex.
    for (const dead of [
      '"Fira Code', // unbalanced quote
      "Fira Code\", monospace", // quote inside an unquoted family
      "Menlo,,monospace", // empty segment
      ",monospace", // leading empty segment
      "monospace,", // trailing empty segment
      "   ", // whitespace only
      '""', // an empty quoted family names nothing
    ]) {
      expect(coerceTermFontFamily(dead), dead).toBe(DEFAULT_TERM_FONT_FAMILY);
      expect(isTermFontFamily(dead), dead).toBe(false);
    }
  });

  test("the character allowlist closes the CSS escapes", () => {
    for (const hostile of [
      "Menlo; color: red",
      "url(evil.woff2)",
      "Menlo}\n#x{color:red",
      "</style><script>",
      "Menlo\\3b color:red",
    ]) {
      expect(coerceTermFontFamily(hostile), hostile).toBe(
        DEFAULT_TERM_FONT_FAMILY,
      );
    }
  });

  test("the length cap is enforced AT the boundary, not near it", () => {
    const atCap = "Menlo, " + "a".repeat(TERM_FONT_FAMILY_MAX_LEN - 7);
    expect(atCap).toHaveLength(TERM_FONT_FAMILY_MAX_LEN);
    expect(isTermFontFamily(atCap)).toBe(true);
    expect(isTermFontFamily(atCap + "a")).toBe(false);
    expect(coerceTermFontFamily(atCap + "a")).toBe(DEFAULT_TERM_FONT_FAMILY);
  });

  test("JS-only edges the JSON fixture cannot carry", () => {
    expect(coerceTermFontFamily(undefined)).toBe(DEFAULT_TERM_FONT_FAMILY);
    expect(isTermFontFamily(undefined)).toBe(false);
  });

  test("interior spacing survives; only the ends are trimmed", () => {
    // Load-bearing: the UI decides which card is ACTIVE by string equality against the preset
    // stacks. Any rewriting here would show a preset as Custom.
    expect(coerceTermFontFamily("  Menlo ,  monospace  ")).toBe(
      "Menlo ,  monospace",
    );
  });
});

describe("the preset table", () => {
  test("every preset is a usable stack that ends in the system fallback", () => {
    for (const p of TERM_FONT_PRESETS) {
      expect(isTermFontFamily(p.stack), p.id).toBe(true);
      expect(p.stack.endsWith("monospace"), p.id).toBe(true);
    }
  });

  test("ids and stacks are unique — a duplicate stack would light two cards at once", () => {
    expect(new Set(TERM_FONT_PRESETS.map((p) => p.id)).size).toBe(
      TERM_FONT_PRESETS.length,
    );
    expect(new Set(TERM_FONT_PRESETS.map((p) => p.stack)).size).toBe(
      TERM_FONT_PRESETS.length,
    );
  });

  test("a preset's `primary` is the FIRST family of its own stack", () => {
    // The availability probe is asked about `primary`; if it drifted from the stack the greyed
    // state would describe a font the operator is not actually selecting.
    for (const p of TERM_FONT_PRESETS) {
      if (p.primary === null) continue;
      expect(p.stack.startsWith(`"${p.primary}"`), p.id).toBe(true);
    }
  });

  test("Phase 1 ships NO bundled face — no card may promise an asset this build lacks", () => {
    // The mockups show the combined end state; the bundled card arrives with its .woff2 in
    // Phase 2. Until then every preset must be a face the DEVICE might have, never one we
    // claim to provide.
    expect(TERM_FONT_PRESETS.some((p) => p.note.toLowerCase().includes("bundled"))).toBe(
      false,
    );
  });

  test("presetForStack recognises exactly the preset stacks", () => {
    for (const p of TERM_FONT_PRESETS) {
      expect(presetForStack(p.stack)?.id).toBe(p.id);
    }
    // A custom stack is simply "no preset" — the caller needs no separate flag.
    expect(presetForStack('"Comic Mono", monospace')).toBeUndefined();
  });

  test("System is the default stack, so the default has a home in the grid", () => {
    expect(TERM_FONT_PRESETS[0].stack).toBe(DEFAULT_TERM_FONT_FAMILY);
    expect(TERM_FONT_PRESETS[0].primary).toBeNull();
  });
});

describe("device storage", () => {
  beforeEach(() => localStorage.clear());

  test("round-trips, and a device with no choice is not treated as having one", () => {
    expect(hasStoredTermFontFamily()).toBe(false);
    expect(readStoredTermFontFamily()).toBe(DEFAULT_TERM_FONT_FAMILY);
    storeTermFontFamily('"Fira Code", monospace');
    expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe(
      '"Fira Code", monospace',
    );
    expect(hasStoredTermFontFamily()).toBe(true);
    expect(readStoredTermFontFamily()).toBe('"Fira Code", monospace');
  });

  test("an edited cache cannot strand the terminal in an unrenderable face", () => {
    localStorage.setItem(TERM_FONT_STORAGE_KEY, "Menlo,,monospace");
    expect(readStoredTermFontFamily()).toBe(DEFAULT_TERM_FONT_FAMILY);
    // …and it does not count as a choice, so the server seed still gets its one chance.
    expect(hasStoredTermFontFamily()).toBe(false);
  });

  test("the store never writes a value it would refuse to read back", () => {
    storeTermFontFamily("  Menlo, monospace  ");
    expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe("Menlo, monospace");
    expect(hasStoredTermFontFamily()).toBe(true);
  });
});
