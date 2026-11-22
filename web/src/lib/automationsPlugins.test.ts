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
