import { describe, expect, test } from "vitest";
import type { TemplateVariable } from "../../types/api";
import { inputState, missingRequired, sourceLabels, type Globals } from "./playbookInputState";
import type { Variable } from "./playbookDraft";

const g = (rows: Partial<TemplateVariable>[]): Globals =>
  new Map(
    rows.map((r) => [
      r.name!,
      { kind: "text", created_at: 0, updated_at: 0, used_by: [], ...r } as TemplateVariable,
    ]),
  );
const inherit = { mode: "inherit" as const, value: "" };
const text = (v: Partial<Variable> = {}): Variable => ({ name: "repo", kind: "text", required: true, ...v });
const secret = (v: Partial<Variable> = {}): Variable => ({ name: "tok", kind: "secret", required: true, ...v });

describe("what Default resolves to, before the project exists (#1508)", () => {
  test("a text variable: a saved global wins over the playbook default, as on the server", () => {
    expect(sourceLabels(text({ default: "staging" }), g([{ name: "repo", value: "prod" }]), false).inherit).toBe(
      "Default · prod",
    );
    expect(sourceLabels(text({ default: "staging" }), g([]), false).inherit).toBe("Default · staging");
    expect(sourceLabels(text(), g([]), false).inherit).toBe("No default");
    // Globals unknown: say nothing it cannot know.
    expect(sourceLabels(text(), null, false)).toMatchObject({ inherit: "Default", global: "Global" });
  });

  test("a secret never falls back to a global on its own; Global names whether one is set", () => {
    const labels = sourceLabels(secret(), g([{ name: "tok", kind: "secret", set: true }]), false);
    expect(labels).toMatchObject({ inherit: "No default", global: "Global · set" });
    expect(sourceLabels(secret(), g([{ name: "tok", kind: "secret", set: true, needs_reentry: true }]), false).global).toBe(
      "Global · none saved",
    );
  });

  test("a long value is cut to one short line", () => {
    const long = "https://git.example.com/a/very/long/path/to/a/repository.git";
    expect(sourceLabels(text(), g([{ name: "repo", value: long }]), false).global).toMatch(/^Global · .{27}…$/);
  });
});

describe("definitely missing — and only then (#1508)", () => {
  test("value: empty is missing, whitespace too", () => {
    expect(inputState(text(), { mode: "value", value: "  " }, g([]), false)).toBe("missing");
    expect(inputState(text(), { mode: "value", value: "x" }, g([]), false)).toBe("ok");
  });

  test("inherit: text needs a global or a default; a secret is missing before the project exists", () => {
    expect(inputState(text(), inherit, g([]), false)).toBe("missing");
    expect(inputState(text({ default: "s" }), inherit, null, false)).toBe("ok");
    expect(inputState(text(), inherit, g([{ name: "repo", value: "v" }]), false)).toBe("ok");
    expect(inputState(text(), inherit, null, false)).toBe("unknown");
    expect(inputState(secret(), inherit, g([{ name: "tok", kind: "secret", set: true }]), false)).toBe("missing");
  });

  test("global: missing only when the globals were read and hold none usable", () => {
    expect(inputState(text(), { mode: "global", value: "" }, g([]), false)).toBe("missing");
    expect(inputState(text(), { mode: "global", value: "" }, g([{ name: "repo", kind: "secret", set: true }]), false)).toBe(
      "missing",
    );
    expect(inputState(text(), { mode: "global", value: "" }, null, false)).toBe("unknown");
  });

  test("once the project exists a saved binding may resolve inherit: never held", () => {
    expect(inputState(secret(), inherit, g([]), true)).toBe("unknown");
    expect(sourceLabels(secret(), g([]), true).inherit).toBe("Saved");
  });

  test("missingRequired lists only required inputs that are definitely missing", () => {
    const vars = [text(), text({ name: "opt", required: false }), secret(), text({ name: "d", default: "x" })];
    expect(missingRequired(vars, {}, g([]), false)).toEqual(["repo", "tok"]);
    expect(missingRequired(vars, {}, null, false)).toEqual(["tok"]);
    expect(missingRequired(vars, { tok: { mode: "value", value: "s" } }, g([]), true)).toEqual([]);
  });
});
