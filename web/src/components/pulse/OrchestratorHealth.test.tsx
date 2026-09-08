/** #930 review 1, completion note — the health badge is tested against ITSELF.
 *
 *  `Orchestrator.test.tsx` still passes, but it exercises a component this PR unmounts, so it
 *  says nothing about what the console now renders. These cover the three answers that matter:
 *  silence when healthy, silence on ONE failure (a blip that shouts is a badge people learn to
 *  ignore), and — the one #772 exists for — recovery clearing the warning when newer evidence
 *  arrives, rather than the badge outliving the outage it describes. */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrchestratorHealth } from "./OrchestratorHealth";
import { api } from "../../lib/api";

const NOW = Math.floor(Date.now() / 1000);

function state(last: Record<string, unknown> | undefined) {
  return { last: last ? { orchestrator: last } : {} } as never;
}

function mount(refreshKey = 0) {
  return render(
    <MemoryRouter>
      <OrchestratorHealth refreshKey={refreshKey} />
    </MemoryRouter>,
  );
}

describe("OrchestratorHealth", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("renders nothing while the orchestrator is healthy", async () => {
    vi.spyOn(api, "orchestrator").mockResolvedValue(
      state({ ok: true, consecutive_failures: 0, last_ok: NOW - 60 }),
    );
    mount();
    await waitFor(() => expect(api.orchestrator).toHaveBeenCalled());
    expect(screen.queryByTestId("orch-degraded")).toBeNull();
  });

  it("stays silent on a single failure — the outage bar is server-owned", async () => {
    vi.spyOn(api, "orchestrator").mockResolvedValue(
      state({ ok: false, consecutive_failures: 1, last_ok: NOW - 600 }),
    );
    mount();
    await waitFor(() => expect(api.orchestrator).toHaveBeenCalled());
    expect(screen.queryByTestId("orch-degraded")).toBeNull();
  });

  it("names the outage, and points at the panel that can fix it", async () => {
    vi.spyOn(api, "orchestrator").mockResolvedValue(
      state({
        ok: false,
        consecutive_failures: 2,
        last_ok: NOW - 3600,
        error: "connection refused",
      }),
    );
    mount();
    const badge = await screen.findByTestId("orch-degraded");
    expect(badge).toHaveTextContent(/can’t reach its AI endpoint/i);
    expect(badge).toHaveTextContent("connection refused");
    expect(screen.getByRole("link", { name: /settings/i })).toHaveAttribute(
      "href",
      "/settings/ai-review",
    );
  });

  it("CLEARS once a newer pass succeeds — the recovery half (#772)", async () => {
    // The badge must not outlive the outage: a pass run from Settings is newer evidence, and
    // the console is told about it through `refreshKey`. Without the refetch the warning would
    // survive the very action the operator is told to take to clear it.
    const spy = vi
      .spyOn(api, "orchestrator")
      .mockResolvedValueOnce(
        state({ ok: false, consecutive_failures: 2, last_ok: NOW - 3600 }),
      )
      .mockResolvedValueOnce(
        state({ ok: true, consecutive_failures: 0, last_ok: NOW }),
      );

    const { rerender } = mount(1);
    await screen.findByTestId("orch-degraded");

    rerender(
      <MemoryRouter>
        <OrchestratorHealth refreshKey={2} />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.queryByTestId("orch-degraded")).toBeNull(),
    );
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("degrades to no badge when the endpoint throws, never to a crash", async () => {
    vi.spyOn(api, "orchestrator").mockRejectedValue(new Error("500"));
    mount();
    await waitFor(() => expect(api.orchestrator).toHaveBeenCalled());
    expect(screen.queryByTestId("orch-degraded")).toBeNull();
  });
});
