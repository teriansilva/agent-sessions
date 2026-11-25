/** "Run a command" / "Run a script" in the wizard's form (#2248 P3): every row is sent exactly as
 *  written, a delivery value is ONE whole `{from}` element on the action, the program is always
 *  fixed, and a stored one reads back into the same form. */
import { describe, expect, it } from "vitest";

import type { Automation } from "../types/automations";
import { blankForm, formAction, formFromAutomation, stepProblems, type EditorForm } from "./automations";

function form(over: Partial<EditorForm>): EditorForm {
  return { ...blankForm("UTC"), ...over };
}
const hook = { triggerKind: "webhook" as const, webhookId: "0a1b2c3d4e5f6a7b" };
const pr = { path: "pull_request.number", type: "number" as const, choices: [] };

describe("run_command", () => {
  it("sends the program as a fixed argv[0], each row as one element, options kept", () => {
    const f = form({
      actionKind: "run_command",
      projectId: "p1",
      runProgram: " make ",
      runArgs: [
        { value: "-C", option: true, map: null },
        { value: "site", option: false, map: null },
        { value: "a b; $(c)", option: false, map: null },
      ],
      runEnv: [{ name: "MODE", value: "fast", map: null }],
      runTimeout: 7,
      runOutputs: [{ name: "build_id", type: "text" }],
    });
    expect(formAction(f)).toEqual({
      kind: "plugin",
      plugin: "run_command",
      version: 1,
      inputs: {
        project_id: "p1",
        argv: [{ value: "make" }, { value: "-C", option: true }, { value: "site" }, { value: "a b; $(c)" }],
        env: [{ name: "MODE", value: "fast" }],
        timeout_minutes: 7,
        outputs: { build_id: "text" },
      },
    });
  });

  it("puts a delivery value in its own element and variable, mapped on the action", () => {
    const f = form({
      ...hook,
      actionKind: "run_command",
      projectId: "p1",
      runProgram: "make",
      runArgs: [
        { value: "--", option: false, map: null },
        { value: "", option: false, map: pr },
      ],
      runEnv: [{ name: "PR_NUMBER", value: "", map: pr }],
    });
    const a = formAction(f) as { inputs: Record<string, unknown>; from_delivery: unknown };
    expect(a.inputs.argv).toEqual([{ value: "make" }, { value: "--" }, { from: "arg_2" }]);
    expect(a.inputs.env).toEqual([{ name: "PR_NUMBER", from: "env_pr_number" }]);
    expect(a.from_delivery).toEqual({
      arg_2: { path: "pull_request.number", type: "number" },
      env_pr_number: { path: "pull_request.number", type: "number" },
    });
  });

  it("reads a stored one back into the same form", () => {
    const action = {
      kind: "plugin" as const,
      plugin: "run_command" as const,
      version: 1 as const,
      inputs: {
        project_id: "p1",
        argv: [{ value: "make" }, { value: "--" }, { from: "arg_2" }],
        env: [{ name: "PR", from: "env_pr" }],
        timeout_minutes: 5,
        outputs: {},
      },
      from_delivery: {
        arg_2: { path: "pull_request.number", type: "number" as const },
        env_pr: { path: "pull_request.number", type: "number" as const },
      },
    };
    const f = formFromAutomation(
      { name: "n", trigger: { kind: "webhook", webhook_id: "0a1b2c3d4e5f6a7b" }, action, policy: null } as unknown as Automation,
      "UTC",
    );
    expect(f.runProgram).toBe("make");
    expect(f.runArgs.map((a) => a.map?.path ?? a.value)).toEqual(["--", "pull_request.number"]);
    // The env's own field name is re-derived from its variable name; the mapping survives.
    const again = formAction(f) as { inputs: Record<string, unknown> };
    expect(again.inputs.argv).toEqual(action.inputs.argv);
  });

  it("holds the step for a missing program, a relative path, or a delivery row after an option", () => {
    const what = (over: Partial<EditorForm>) =>
      stepProblems(form({ actionKind: "run_command", projectId: "p1", runProgram: "make", ...over })).what;
    expect(what({ runProgram: "" })).toContain("Name the program to run");
    expect(what({ runProgram: "./x.sh" }).join()).toContain("use Run a script");
    expect(
      what({
        ...hook,
        runArgs: [
          { value: "-c", option: true, map: null },
          { value: "", option: false, map: pr },
        ],
      }).join(),
    ).toContain("is the value of the option -c");
    // A row starting with - is an option, flag or no flag (#2279 review): the next row is fixed.
    expect(
      what({
        ...hook,
        runArgs: [
          { value: "-e", option: false, map: null },
          { value: "", option: false, map: pr },
        ],
      }).join(),
    ).toContain("is the value of the option -e");
    expect(
      what({
        ...hook,
        runArgs: [
          { value: "--", option: false, map: null },
          { value: "", option: false, map: pr },
        ],
      }),
    ).toEqual([]);
    expect(what({ runEnv: [{ name: "PATH", value: "/x", map: null }] }).join()).toContain("set by the run itself");
    expect(what({ runTimeout: 31 }).join()).toContain("1 to 30 minutes");
    expect(what({ runOutputs: [{ name: "exit_code", type: "number" }] }).join()).toContain(
      "exit_code is always reported",
    );
    expect(what({})).toEqual([]);
  });
});

describe("run_script", () => {
  it("sends the script path and its extra arguments", () => {
    const f = form({
      actionKind: "run_script",
      projectId: "p1",
      runScript: "scripts/deploy.sh",
      runArgs: [{ value: "--fast", option: false, map: null }],
    });
    expect(formAction(f)).toEqual({
      kind: "plugin",
      plugin: "run_script",
      version: 1,
      inputs: {
        project_id: "p1",
        script: "scripts/deploy.sh",
        args: [{ value: "--fast" }],
        env: [],
        timeout_minutes: 5,
        outputs: {},
      },
    });
  });

  it("holds the step for a script outside the project's folder", () => {
    const what = (s: string) =>
      stepProblems(form({ actionKind: "run_script", projectId: "p1", runScript: s })).what.join();
    expect(what("")).toContain("Pick the script");
    expect(what("/etc/x.sh")).toContain("inside the project’s folder");
    expect(what("../x.sh")).toContain("must stay inside");
    expect(what(".hidden/x.sh")).toContain("not be hidden");
    expect(what("scripts/x.sh")).toBe("");
  });
});
