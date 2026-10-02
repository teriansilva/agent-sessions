/** The automations list read (#1252 review): only the newest-started read applies, and a
 *  mutation's own row applies at once and voids reads started before it. */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { Automation, AutomationList } from "../../types/automations";
import { useAutomations } from "./useAutomations";

afterEach(() => vi.restoreAllMocks());

function list(state: string): AutomationList {
  return {
    automations: [{ id: "a1", name: "n", state, enabled: state !== "off" } as Automation],
    loop: { enabled: true, owner: true },
    limits: {} as AutomationList["limits"],
  };
}

test("a slow read that started before an Enable never paints the old state back", async () => {
  const answers: ((v: AutomationList) => void)[] = [];
  vi.spyOn(api, "automations").mockImplementation(
    () => new Promise<AutomationList>((res) => answers.push(res)),
  );
  const { result } = renderHook(() => useAutomations());
  await waitFor(() => expect(answers).toHaveLength(1)); // the mount read
  answers[0](list("off"));
  await waitFor(() => expect(result.current.data?.automations[0].state).toBe("off"));

  let slow: Promise<unknown> = Promise.resolve();
  act(() => {
    slow = result.current.reload(); // a focus read, started BEFORE the Enable…
  });
  act(() => result.current.applyRow({ id: "a1", name: "n", state: "enabled", enabled: true } as Automation));
  expect(result.current.data?.automations[0].state).toBe("enabled"); // applied at once
  let fresh: Promise<unknown> = Promise.resolve();
  act(() => {
    fresh = result.current.reload(); // …and the reload that followed the Enable
  });
  answers[2](list("enabled")); // the newer read lands first
  await act(() => fresh);
  answers[1](list("off")); // the older one lands last, carrying the old state
  await act(() => slow);
  expect(result.current.data?.automations[0].state).toBe("enabled");
});
