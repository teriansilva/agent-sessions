/** The mission state dot, shared by the rail, the header chip and the thread chips (#967).
 *
 *  Status colour is load-bearing (docs/design.md §3): green says work is underway. A launch still in
 *  flight has not earned it, so `dispatching` ("starting") is a neutral `--text-2` dot and only
 *  `running` is green. The class is pinned here, and the colour behind each class is read from the
 *  stylesheet itself, so neither half can drift alone. The browser half is in
 *  e2e/mission-thread-events.spec.ts. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "vitest";

import styles from "./mission.module.css";
import { missionDotClass, missionStateLabel } from "./missionState";

const CSS = readFileSync(
  resolve(process.cwd(), "src/components/pulse/mission.module.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the first top-level rule for exactly `.name`. */
function rule(name: string): string {
  const m = CSS.match(new RegExp(`(^|\\n)\\.${name}\\s*\\{([^}]*)\\}`));
  expect(m, `.${name} is declared`).not.toBeNull();
  return m![2];
}

test("starting is the neutral dot and running is the green one", () => {
  expect(styles.dotStarting).toBeTruthy();
  expect(styles.dotRunning).toBeTruthy();
  expect(missionDotClass("dispatching")).toBe(styles.dotStarting);
  expect(missionDotClass("dispatching")).not.toBe(styles.dotRunning);
  expect(missionDotClass("running")).toBe(styles.dotRunning);
  expect(missionStateLabel("dispatching")).toBe("starting");

  expect(rule("dotStarting")).toMatch(/background:\s*var\(--text-2\)/);
  expect(rule("dotStarting")).not.toMatch(/--status-/);
  expect(rule("dotRunning")).toMatch(/background:\s*var\(--status-up\)/);
});

test("the other states keep their dots", () => {
  expect(missionDotClass("failed")).toBe(styles.dotFailed);
  expect(missionDotClass("done")).toBe(styles.dotDone);
  expect(missionDotClass("abandoned")).toBe(styles.dotDone);
  expect(missionDotClass("planned")).toBe(styles.dot);
  expect(missionDotClass("draft")).toBe(styles.dot);
});
