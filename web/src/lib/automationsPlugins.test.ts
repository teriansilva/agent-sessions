/** Plugin actions in the wizard's form (#2248 P2): what the form sends is exactly the stored plugin
 *  shape, a delivery's mapping rides on the ACTION, and a stored one reads back into the same form. */
import { describe, expect, it } from "vitest";

import type { ActionEntry } from "../types/actions";
import type { Automation } from "../types/automations";
import { needsSetup } from "./actions";
import { blankForm, formAction, formFromAutomation, stepProblems, type EditorForm } from "./automations";

function form(over: Partial<EditorForm>): EditorForm {
  return { ...blankForm("UTC"), ...over };
}

describe("send_message", () => {
  it("sends the plugin shape with the connector, the recipient and the message", () => {
    const f = form({ actionKind: "send_message", connector: "c_0123456789ab", recipient: "42", text: "hi" });
    expect(formAction(f)).toEqual({
      kind: "plugin",
      plugin: "send_message",
      version: 1,
      inputs: { connector: "c_0123456789ab", recipient: "42", message: { text: "hi" } },
    });
  });

  it("puts a delivery's mapping on the action, never inside the message", () => {
    const f = form({
      actionKind: "send_message",
      triggerKind: "webhook",
      webhookId: "0a1b2c3d4e5f6a7b",
      connector: "c_0123456789ab",
      recipient: "42",
      messageMode: "template",
      templateId: "t1",
      values: { title: "x" },
      fromDelivery: { number: { path: "pull_request.number", type: "number", choices: [] } },
    });
    const a = formAction(f);
    expect(a).toMatchObject({
      inputs: { message: { template_id: "t1", values: { title: "x" } } },
      from_delivery: { number: { path: "pull_request.number", type: "number" } },
    });
    expect(JSON.stringify((a as { inputs: unknown }).inputs)).not.toContain("from_delivery");
  });

  it("reads a stored one back into the same form", () => {
    const action = {
      kind: "plugin" as const,
      plugin: "send_message" as const,
      version: 1 as const,
      inputs: { connector: "c_0123456789ab", recipient: "-1009", message: { text: "done" } },
    };
    const f = formFromAutomation({ name: "n", trigger: { kind: "manual" }, action, policy: null } as unknown as Automation, "UTC");
    expect(f.actionKind).toBe("send_message");
    expect(f.connector).toBe("c_0123456789ab");
    expect(f.recipient).toBe("-1009");
    expect(f.text).toBe("done");
    expect(formAction(f)).toEqual(action);
  });

  it("holds the step until a messenger and a recipient are chosen", () => {
    expect(stepProblems(form({ actionKind: "send_message", text: "x" })).what).toContain(
      "Choose what to send with",
    );
    expect(
      stepProblems(form({ actionKind: "send_message", text: "x", connector: "c_0123456789ab" })).what,
    ).toContain("Choose who receives it");
  });
});

describe("needsSetup", () => {
  const base: ActionEntry = {
    id: "send_message", label: "Send a message", category: "messaging", description: "",
    effect: "notify", executor: "send_message", triggers: [], inputs: [], outputs: [],
    requires: [], available: true, used_by: 0,
  };  // prettier-ignore
  it("blocks only a requirement checked and not met, with where it is set up", () => {
    expect(needsSetup(base)).toBeNull();
    const open = { ...base, requires: [{ id: "memory", words: "memory on", ok: null, detail: "" }] };
    expect(needsSetup(open)).toBeNull();
    const missing = {
      ...base,
      requires: [{ id: "messenger", words: "a messenger", ok: false, detail: "needs Telegram", setup: "ai-hq" }],
    };
    expect(needsSetup(missing)).toEqual({ words: "Needs Telegram", setup: "ai-hq" });
  });
});

describe("call_webhook", () => {
  const hookForm = (over: Partial<EditorForm> = {}) =>
    form({
      actionKind: "call_webhook",
      destination: " https://hooks.example.org/x ",
      bodyFields: [
        { name: "job", value: "nightly", map: null },
        { name: "number", value: "", map: { path: "pull_request.number", type: "number", choices: [] } },
      ],
      ...over,
    });

  it("sends the fixed fields as inputs and a mapped one only from a webhook from the list", () => {
    expect(formAction(hookForm())).toEqual({
      kind: "plugin",
      plugin: "call_webhook",
      version: 1,
      inputs: { destination: "https://hooks.example.org/x", allow_private: false, fields: { job: "nightly" } },
    });
    const hooked = formAction(hookForm({ triggerKind: "webhook", webhookId: "0a1b2c3d4e5f6a7b" }));
    expect(hooked).toMatchObject({
      from_delivery: { number: { path: "pull_request.number", type: "number" } },
    });
  });

  it("reads a stored one back, fixed and mapped fields both", () => {
    const action = formAction(hookForm({ triggerKind: "webhook", webhookId: "0a1b2c3d4e5f6a7b", allowPrivate: true }));
    const f = formFromAutomation(
      { name: "n", trigger: { kind: "webhook", webhook_id: "0a1b2c3d4e5f6a7b" }, action, policy: null } as unknown as Automation,
      "UTC",
    );
    expect(f.allowPrivate).toBe(true);
    expect(formAction(f)).toEqual(action);
  });

  it("asks for an https address with no credentials and well-named fields", () => {
    const what = (over: Partial<EditorForm>) => stepProblems(hookForm(over)).what;
    expect(what({ destination: "http://x.example.org" })).toContain("The address must start with https://");
    expect(what({ destination: "https://u:p@x.example.org" })).toContain(
      "The address must not carry a user name or password",
    );
    expect(what({ bodyFields: [{ name: "Bad", value: "", map: null }] })[0]).toMatch(/needs a name/);
    expect(
      what({ bodyFields: [{ name: "a", value: "", map: null }, { name: "a", value: "", map: null }] }),
    ).toContain("Two fields have the same name");
    expect(what({})).toEqual([]);
  });
});

describe("publish_artifact", () => {
  const art = (over: Partial<EditorForm> = {}) =>
    form({
      actionKind: "publish_artifact",
      projectId: "p1",
      artifactPath: " reports/nightly.md ",
      artifactTitle: "Nightly",
      artifactWhy: "",
      ...over,
    });

  it("sends the project, the relative path and the lines; a mapped line goes on the action", () => {
    expect(formAction(art())).toEqual({
      kind: "plugin",
      plugin: "publish_artifact",
      version: 1,
      inputs: { project_id: "p1", path: "reports/nightly.md", title: "Nightly", why: "" },
    });
    const mapped = formAction(
      art({
        triggerKind: "webhook",
        webhookId: "0a1b2c3d4e5f6a7b",
        lineMaps: { title: { path: "pull_request.title", type: "text", choices: [] }, why: null },
      }),
    );
    expect(mapped).toMatchObject({
      inputs: { title: "" },
      from_delivery: { title: { path: "pull_request.title", type: "text" } },
    });
    const back = formFromAutomation(
      { name: "n", trigger: { kind: "webhook", webhook_id: "0a1b2c3d4e5f6a7b" }, action: mapped, policy: null } as unknown as Automation,
      "UTC",
    );
    expect(formAction(back)).toEqual(mapped);
  });

  it("refuses an absolute path, a climb out and a missing title before the server does", () => {
    const what = (over: Partial<EditorForm>) => stepProblems(art(over)).what;
    expect(what({ artifactPath: "/etc/passwd" })).toContain("The path is relative to the project’s folder");
    expect(what({ artifactPath: "a/../../b" })).toContain("The path must stay inside the project’s folder");
    expect(what({ artifactTitle: " " })).toContain("Give it a title");
    expect(what({})).toEqual([]);
  });
});
