import { describe, expect, test } from "vitest";
import {
  assembleMessage,
  defaultValues,
  missingLibrary,
  missingRequired,
  previewValues,
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
