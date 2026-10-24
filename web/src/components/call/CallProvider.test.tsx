/** The call provider's capabilities: only the newest read lands (#1640 review 6916). */
import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { VoiceCapabilities } from "../../types/voice";
import { useCall } from "./callContext";
import { CallProvider } from "./CallProvider";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { voiceCapabilities: vi.fn() } };
});
vi.mock("../hq/useHqContact", () => ({ useHqContact: () => ({ wire: "off" }) }));

const caps = (configured: boolean) => ({ configured, available: configured }) as unknown as VoiceCapabilities;

/** Each read answers when the test says so, in any order. */
let pending: Array<(c: VoiceCapabilities) => void> = [];
beforeEach(() => {
  pending = [];
  vi.mocked(api.voiceCapabilities).mockReset();
  vi.mocked(api.voiceCapabilities).mockImplementation(() => new Promise((r) => pending.push(r)));
});

function Probe() {
  const { caps: c, refreshCaps } = useCall();
  return (
    <>
      <output data-testid="configured">{c === null ? "unknown" : String(c.configured)}</output>
      <button onClick={refreshCaps}>refresh</button>
    </>
  );
}

for (const [older, newer] of [
  [false, true], // Settings opened while calls were unconfigured, then a save made them available
  [true, false], // the reverse edit
] as const) {
  test(`an older read answering last never undoes a newer one (${older} → ${newer})`, async () => {
    render(
      <CallProvider>
        <Probe />
      </CallProvider>,
    );
    await vi.waitFor(() => expect(pending).toHaveLength(1)); // the read on mount
    act(() => screen.getByRole("button", { name: "refresh" }).click()); // the read after a save
    await vi.waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1](caps(newer)));
    expect(screen.getByTestId("configured")).toHaveTextContent(String(newer));
    await act(async () => pending[0](caps(older))); // the older read lands late
    expect(screen.getByTestId("configured")).toHaveTextContent(String(newer));
  });
}
