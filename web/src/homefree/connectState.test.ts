import { beforeEach, expect, test } from "vitest";

import { renderConnectState } from "./connectState";

let host: HTMLElement;
beforeEach(() => {
  host = document.createElement("div");
});

test("loading renders the box, step checklist, progress rail, and blind-relay note", () => {
  renderConnectState(host, {
    kind: "loading",
    box: "nightjar",
    steps: [
      { label: "human verification", state: "done" },
      { label: "loading app shell", state: "active" },
    ],
    progress: 60,
  });
  expect(host.dataset.state).toBe("loading");
  expect(host.querySelector(".cs-title")?.textContent).toContain("nightjar");
  expect(host.querySelectorAll(".cs-step").length).toBe(2);
  expect((host.querySelector(".cs-rail-fill") as HTMLElement).style.width).toBe("60%");
  expect(host.querySelector(".cs-note")?.textContent).toMatch(/relay is blind/);
});

test("error shows the message with a RETRY action", () => {
  let retried = 0;
  renderConnectState(
    host,
    { kind: "error", message: "connection_failed" },
    { onRetry: () => retried++ },
  );
  expect(host.dataset.state).toBe("error");
  expect(host.querySelector(".cs-message")?.textContent).toBe("connection_failed");
  const btns = [...host.querySelectorAll("button")];
  expect(btns.map((b) => b.textContent)).toEqual(["RETRY"]);
  btns.find((b) => b.textContent === "RETRY")?.click();
  expect(retried).toBe(1);
});

test("an action with no handler renders a disabled button (no dead click target)", () => {
  renderConnectState(host, { kind: "error", message: "x" }); // no actions passed
  const btns = [...host.querySelectorAll("button")] as HTMLButtonElement[];
  expect(btns.length).toBeGreaterThan(0);
  expect(btns.every((b) => b.disabled)).toBe(true);
});
