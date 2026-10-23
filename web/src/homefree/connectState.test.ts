import { beforeEach, expect, test } from "vitest";

import { CONNECT_STEPS, connectSteps, errorState, renderConnectState } from "./connectState";

let host: HTMLElement;
beforeEach(() => {
  host = document.createElement("div");
});

const states = () => [...host.querySelectorAll(".cs-step")].map((li) => li.className.replace("cs-step cs-step-", ""));

test("loading renders the box, the radar, the step list, the acquire bar and the blind-relay note", () => {
  renderConnectState(host, { kind: "loading", box: "nightjar", steps: connectSteps(1, "active") });
  expect(host.dataset.state).toBe("loading");
  expect(host.querySelector(".nj-callsign")?.textContent).toContain("nightjar");
  expect(host.querySelector(".nj-radar")).not.toBeNull();
  expect(host.querySelector(".nj-acquire")).not.toBeNull();
  expect(states()).toEqual(["done", "active", "pending", "pending"]);
  // The step's state is in words too, not only in its LED's colour.
  expect(host.querySelector(".cs-step-active")?.textContent).toContain("in progress");
  expect(host.querySelector(".cs-note")?.textContent).toMatch(/relay is blind/);
  expect(host.querySelectorAll("button")).toHaveLength(0);
});

test("the error card shows the failed step, the message, and Back beside Retry", () => {
  let retried = 0;
  let back = 0;
  renderConnectState(host, errorState("offline", "viper-8231", 2), {
    onRetry: () => retried++,
    onBack: () => back++,
  });
  expect(host.dataset.state).toBe("error");
  expect(host.querySelector(".cs-title")?.textContent).toBe("Your box is offline");
  expect(host.querySelector(".nj-callsign")?.textContent).toContain("viper-8231");
  expect(states()).toEqual(["done", "done", "failed", "pending"]);
  expect(host.querySelector(".cs-step-failed .hud-led.down")).not.toBeNull();
  const btns = [...host.querySelectorAll("button")];
  expect(btns.map((b) => b.textContent)).toEqual(["Back", "Retry ▸"]);
  btns[1].click();
  btns[0].click();
  expect([retried, back]).toEqual([1, 1]);
});

test("an action with no handler renders a disabled button (no dead click target)", () => {
  renderConnectState(host, errorState("protocol", "x", 1)); // no actions passed
  const btns = [...host.querySelectorAll("button")] as HTMLButtonElement[];
  expect(btns.length).toBe(2);
  expect(btns.every((b) => b.disabled)).toBe(true);
});

test.each([
  ["captcha_unsolvable", 0, "Relay challenge failed"],
  ["connection_failed", 1, "Relay unreachable"],
  ["captcha_failed", 1, "Verification refused"],
  ["rate_limited", 1, "Too many attempts"],
  ["offline", 2, "Your box is offline"],
  ["busy", 2, "Your box is busy"],
  ["agent_unreachable", 2, "Your box didn't answer"],
  ["handshake", 3, "Access password didn't match"],
])("%s fails at step %i: %s", (code, step, title) => {
  const s = errorState(code, "box", 0);
  expect(s.kind === "error" && s.title).toBe(title);
  expect(s.steps?.findIndex((x) => x.state === "failed")).toBe(step);
});

test("an unknown code fails at the step in flight and names itself", () => {
  const before = errorState("TypeError: Failed to fetch", "box", 0);
  expect(before.kind === "error" && before.title).toBe("Relay unreachable");
  const after = errorState("Error: mount failed", "box", 3);
  expect(after.kind === "error" && after.message).toContain("Error: mount failed");
  expect(after.steps?.map((s) => s.state)).toEqual(["done", "done", "done", "failed"]);
  expect(CONNECT_STEPS).toHaveLength(4);
});
