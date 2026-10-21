import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import {
  assembleMessage,
  defaultValues,
  hasSecret,
  maskedValues,
  missingLibrary,
  missingRequired,
  previewValues,
  shortSecrets,
  splitLibrary,
  renderTemplate,
  seedValues,
  substituteFields,
  tokensIn,
  uploadStoredName,
  unknownTokens,
} from "./templateMessage";

/** `Compose.send()`'s assembly as it was written before the helper existed (#619 era). The
 *  helper must be byte-identical to it for every input — that is what makes the editor
 *  preview an honest statement of what the paste will carry. */
function legacyComposeAssembly(text: string, attachments: { path: string }[]): string {
  const parts: string[] = [];
  if (text.trim()) parts.push(text.trim());
  for (const a of attachments) parts.push(a.path);
  return parts.join(" ");
}

describe("assembleMessage", () => {
  const cases: [string, string[]][] = [
    ["hello", []],
    ["  padded  \n", ["/u/.agent-sessions/uploads/1-a.png"]],
    ["", ["/u/.agent-sessions/uploads/1-a.png", "/u/.agent-sessions/uploads/2-b.png"]],
    ["   ", []],
    ["multi\nline\n", ["/p q.png"]],
  ];
  test.each(cases)("matches Compose.send() for %j + %j", (text, paths) => {
    expect(assembleMessage(text, paths)).toBe(
      legacyComposeAssembly(
        text,
        paths.map((path) => ({ path })),
      ),
    );
  });
  test("paths ride raw and unquoted, after the text", () => {
    expect(assembleMessage("go", ["/a b.png"])).toBe("go /a b.png");
  });
});

describe("tokens", () => {
  test("distinct, in order, server-shaped only", () => {
    expect(tokensIn("{{a}} {{b_2}} {{a}} {{Bad}} {{ spaced }} {{9x}}")).toEqual(["a", "b_2"]);
  });
  test("unknown = not declared", () => {
    expect(unknownTokens("{{pr_url}} {{ghost}}", [{ name: "pr_url" }])).toEqual(["ghost"]);
  });
});

describe("substituteFields", () => {
  const fields = [{ name: "pr_url" }, { name: "issue_ref", default: "the linked issue" }];
  test("replaces declared tokens with values, leaves undeclared and unvalued tokens", () => {
    expect(
      substituteFields("PR {{pr_url}} for {{issue_ref}} ({{ghost}})", fields, {
        pr_url: "https://x/1",
      }),
    ).toBe("PR https://x/1 for {{issue_ref}} ({{ghost}})");
  });
  test("an empty string is a value, not an absence", () => {
    expect(substituteFields("[{{pr_url}}]", fields, { pr_url: "" })).toBe("[]");
  });
  test("no expression language: the value is inserted literally, even if it looks like a token", () => {
    expect(substituteFields("{{pr_url}}", fields, { pr_url: "{{issue_ref}}" })).toBe(
      "{{issue_ref}}",
    );
  });
  test("defaultValues carries only non-empty defaults", () => {
    expect(defaultValues(fields)).toEqual({ issue_ref: "the linked issue" });
  });
});

describe("missingRequired", () => {
  const fields = [
    { name: "a", required: true },
    { name: "b", required: true, default: "dflt" },
    { name: "c", required: false },
  ];
  test("a default satisfies a required field; whitespace does not", () => {
    expect(missingRequired(fields, {})).toEqual(["a"]);
    expect(missingRequired(fields, { a: "   " })).toEqual(["a"]);
    expect(missingRequired(fields, { a: "x", b: "" })).toEqual(["b"]);
  });
});

describe("renderTemplate", () => {
  test("substituted body then image paths — the exact paste", () => {
    const t = {
      body: "Review {{pr_url}}\n",
      fields: [{ name: "pr_url" }],
      images: [{ path: "/u/.agent-sessions/uploads/1-a.png" }],
    };
    expect(renderTemplate(t, { pr_url: "https://x/1" })).toBe(
      "Review https://x/1 /u/.agent-sessions/uploads/1-a.png",
    );
  });
});

test("uploadStoredName is the last path component", () => {
  expect(uploadStoredName("/u/.agent-sessions/uploads/20260903-1-a.png")).toBe(
    "20260903-1-a.png",
  );
  expect(uploadStoredName("bare.png")).toBe("bare.png");
});

describe("library fields (#1090)", () => {
  const fields = [
    { name: "host", default: "", source: "library" as const },
    { name: "cmd", default: "", source: "library" as const },
    { name: "who", default: "you", source: "template" as const },
    { name: "why", default: "" },
  ];
  const lib = { host: "staging.acme.test" };

  test("the preview takes a library field's value from the library, never its default", () => {
    expect(previewValues(fields, lib)).toEqual({ host: "staging.acme.test", who: "you" });
    // A missing variable keeps its token visible instead of previewing a blank.
    expect(substituteFields("{{host}} {{cmd}} {{who}}", fields, previewValues(fields, lib))).toBe(
      "staging.acme.test {{cmd}} you",
    );
  });

  test("the picker seeds library fields from the library and a missing one empty", () => {
    expect(seedValues(fields, lib)).toEqual({ host: "staging.acme.test", cmd: "", who: "you", why: "" });
  });

  test("missingLibrary names only library fields with no variable", () => {
    expect(missingLibrary(fields, lib)).toEqual(["cmd"]);
    expect(missingLibrary(fields, { host: "h", cmd: "c" })).toEqual([]);
    // A template field is never "missing" whatever the library holds.
    expect(missingLibrary([{ name: "who" }], {})).toEqual([]);
  });

  test("an inherited Object property is not a library variable", () => {
    expect(missingLibrary([{ name: "constructor", source: "library" }], {})).toEqual([
      "constructor",
    ]);
  });
});

describe("secret fields (#1090 Phase 2)", () => {
  const fields = [
    { name: "host", source: "library" as const, kind: "text" as const },
    { name: "db_pass", source: "library" as const, kind: "secret" as const, required: true },
    { name: "token", source: "template" as const, kind: "secret" as const, required: true },
    { name: "ticket", required: true },
  ];

  test("splitLibrary keeps secrets as state only, never a value", () => {
    const lib = splitLibrary([
      { name: "host", kind: "text", value: "h" },
      { name: "db_pass", kind: "secret", needs_reentry: false },
      { name: "old", kind: "secret", needs_reentry: true },
    ]);
    expect(lib).toEqual({ text: { host: "h" }, secrets: { db_pass: "ok", old: "reentry" } });
  });

  test("a text field naming a secret, or a secret field naming a text variable, is missing", () => {
    expect(missingLibrary(fields, { db_pass: "not-a-secret" }, { host: "ok" })).toEqual([
      "host",
      "db_pass",
    ]);
    expect(missingLibrary(fields, { host: "h" }, { db_pass: "ok" })).toEqual([]);
    expect(missingLibrary(fields, { host: "h" }, { db_pass: "reentry" })).toEqual(["db_pass"]);
  });

  test("the preview and the history only ever see masks", () => {
    expect(previewValues(fields, { host: "h" })).toMatchObject({
      db_pass: "[secret: db_pass]",
      token: "[secret: token]",
    });
    // The typed value is replaced; the text field is untouched; a stored secret gets its mask.
    expect(maskedValues(fields, { token: "typed-value-1", ticket: "T" })).toEqual({
      token: "[secret: token]",
      ticket: "T",
      db_pass: "[secret: db_pass]",
    });
  });

  test("required-ness skips a stored secret (the server resolves it) but not a typed one", () => {
    expect(missingRequired(fields, { token: "", ticket: "T" })).toEqual(["token"]);
    expect(hasSecret(fields)).toBe(true);
    expect(hasSecret([{ name: "x" }])).toBe(false);
  });

  test("a typed-once secret under the minimum is flagged; a stored one never is", () => {
    expect(shortSecrets(fields, { token: "short" }, 8)).toEqual(["token"]);
    expect(shortSecrets(fields, { token: "long-enough" }, 8)).toEqual([]);
  });
});

describe("render parity with the server (#1090 Phase 2)", () => {
  // The same table `tests/test_template_secrets.py` drives through `template_send`: a template
  // with a secret field is rendered by the SERVER, so its substitution must be this one exactly.
  const cases = JSON.parse(
    readFileSync(resolve(process.cwd(), "../tests/fixtures/template_render_cases.json"), "utf8"),
  ) as {
    name: string;
    body: string;
    fields: string[];
    values: Record<string, string>;
    paths: string[];
    expected: string;
  }[];
  test.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const fields = c.fields.map((name) => ({ name }));
    expect(assembleMessage(substituteFields(c.body, fields, c.values), c.paths)).toBe(c.expected);
  });
});
