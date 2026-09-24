/** The idle window is an operator setting (#768).
 *
 * It replaced a hard-coded 48h constant. Measured on a live store, the median session was
 * 30.4h idle when the orchestrator escalated it — so 48h removed 18% of the notification
 * volume where the 24h default removes 52%. What the panel has to get right is that the
 * chosen value actually reaches the server, and that remounting shows the saved value rather
 * than the pre-save one (the #667 stale-ConfigCtx failure).
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig, OrchestratorConfig } from "../types/api";
import { OrchestratorSettings } from "./OrchestratorSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

vi.mock("../components/pulse/PushDevices", () => ({
  PushDevices: () => null,
}));

function block(over: Partial<OrchestratorConfig> = {}): OrchestratorConfig {
  return {
    enabled: true,
    autonomy: "suggest",
    allowed_verbs: ["continue"],
    auto_verbs_ceiling: ["continue"],
    confidence_min: 0.75,
    interval_minutes: 10,
    max_actions_per_pass: 4,
    proposal_ttl_minutes: 30,
    stale_hours: 24,
    nudge_template: "Please continue.",
    notify: "escalations",
    configured: true,
    default_nudge_template: "Please continue.",
    ...over,
  };
}

function renderPanel(b = block(), refresh: () => void = () => {}) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    orchestrator: b,
  } as unknown as AppConfig;
  return render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={refresh}>
        <ConfigCtx.Provider value={config}>
          <OrchestratorSettings />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockResolvedValue({ orchestrator: block({ stale_hours: 6 }) });
});

test("the window shows the stored value and saves the chosen one as a number", async () => {
  renderPanel(block({ stale_hours: 24 }));
  const select = screen.getByLabelText(/idle for/i);
  expect(select).toHaveValue("24");

  await userEvent.selectOptions(select, "6");
  // A number, not the option's string value — the server rejects a string outright.
  expect(api.setPrefs).toHaveBeenCalledWith({
    orchestrator: { stale_hours: 6 },
  });
});

test("it refreshes the shared config so a remount does not show the pre-save value", async () => {
  const refresh = vi.fn();
  renderPanel(block({ stale_hours: 24 }), refresh);
  await userEvent.selectOptions(screen.getByLabelText(/idle for/i), "48");
  expect(refresh).toHaveBeenCalled();
});

test("a stored value with no matching preset still round-trips", async () => {
  // The server accepts 1..720; the presets are a convenience, not the schema. A value set by
  // hand (or by a future preset) must not silently reset the control to something else.
  renderPanel(block({ stale_hours: 72 }));
  expect(screen.getByLabelText(/idle for/i)).toHaveValue("72");
});

test("the copy says the session stays visible, because that is what makes this safe", () => {
  renderPanel();
  expect(
    screen.getByText(/stays on mission control and in the sidebar/i),
  ).toBeVisible();
});

// --- the slider saved on every drag step (#776) ----------------------------------------------

test("dragging the confidence slider saves ONCE, on release, with the final value", async () => {
  // Measured live: one drag issued 43 `POST /api/prefs` in a single second, each a locked
  // read-modify-write of prefs.json. They serialized into "stuck and slow" and painted a false
  // "Couldn't save" while writes were in fact landing.
  renderPanel(block({ confidence_min: 0.75 }));
  const slider = screen.getByLabelText(/act above confidence/i);

  // Several change events, as a real drag produces.
  fireEvent.change(slider, { target: { value: "0.7" } });
  fireEvent.change(slider, { target: { value: "0.65" } });
  fireEvent.change(slider, { target: { value: "0.6" } });
  expect(api.setPrefs).not.toHaveBeenCalled(); // nothing yet — the drag is local

  fireEvent.pointerUp(slider);
  expect(api.setPrefs).toHaveBeenCalledTimes(1);
  expect(api.setPrefs).toHaveBeenCalledWith({
    orchestrator: { confidence_min: 0.6 },
  });
});

test("the displayed value follows the drag without waiting for the server", async () => {
  renderPanel(block({ confidence_min: 0.75 }));
  const slider = screen.getByLabelText(/act above confidence/i);
  fireEvent.change(slider, { target: { value: "0.55" } });
  expect(screen.getByText("0.55")).toBeInTheDocument();
});

test("a release that changed nothing costs no request", async () => {
  renderPanel(block({ confidence_min: 0.75 }));
  fireEvent.pointerUp(screen.getByLabelText(/act above confidence/i));
  expect(api.setPrefs).not.toHaveBeenCalled();
});

/** The "Act above confidence" readout — scoped to its own row, because the judgment threshold
 *  (#1088) sits on the same panel and also reads 0.90. */
function actValue(text: string) {
  const row = screen.getByLabelText(/act above confidence/i).parentElement!;
  return within(row).getByText(text);
}

test("a stale save response cannot overwrite a newer one", async () => {
  // The defect behind "the slider says 0.70 but the server holds 0.85": responses do not arrive
  // in send order, so applying whichever lands LAST is not applying the last WRITE.
  let releaseFirst: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockImplementationOnce(
      () =>
        new Promise((res) => {
          releaseFirst = res;
        }), // slow: the OLDER save
    )
    .mockResolvedValueOnce({ orchestrator: block({ confidence_min: 0.9 }) });

  renderPanel(block({ confidence_min: 0.75 }));
  const slider = screen.getByLabelText(/act above confidence/i);

  fireEvent.change(slider, { target: { value: "0.6" } });
  fireEvent.pointerUp(slider); // save #1 — in flight
  fireEvent.change(slider, { target: { value: "0.9" } });
  fireEvent.pointerUp(slider); // save #2 — resolves immediately

  await waitFor(() => expect(actValue("0.90")).toBeInTheDocument());
  // …now the older response finally lands, carrying the older value.
  releaseFirst({ orchestrator: block({ confidence_min: 0.6 }) });
  await new Promise((r) => setTimeout(r, 0));
  expect(actValue("0.90")).toBeInTheDocument();
});

test("a stale FAILURE cannot paint an error over a newer success", async () => {
  // The fence originally guarded only successful responses. Start save A, let newer save B
  // succeed, then reject A: A painted "Couldn't save" over B's good state — the same false
  // error this change exists to remove (#776 review).
  let rejectFirst: (e: unknown) => void = () => {};
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockImplementationOnce(
      () =>
        new Promise((_res, rej) => {
          rejectFirst = rej;
        }), // the OLDER save, still pending
    )
    .mockResolvedValueOnce({ orchestrator: block({ confidence_min: 0.9 }) });

  renderPanel(block({ confidence_min: 0.75 }));
  const slider = screen.getByLabelText(/act above confidence/i);

  fireEvent.change(slider, { target: { value: "0.6" } });
  fireEvent.pointerUp(slider); // save A — in flight
  fireEvent.change(slider, { target: { value: "0.9" } });
  fireEvent.pointerUp(slider); // save B — succeeds

  await waitFor(() => expect(actValue("0.90")).toBeInTheDocument());

  rejectFirst(new Error("network died"));
  await new Promise((r) => setTimeout(r, 0));

  expect(screen.queryByText(/couldn’t save/i)).toBeNull();
  expect(actValue("0.90")).toBeInTheDocument();
});

test("a genuine failure on the NEWEST save still reports itself", async () => {
  // The fence must not swallow real errors — it only ignores responses that are out of date.
  vi.mocked(api.setPrefs).mockReset().mockRejectedValue(new Error("nope"));
  renderPanel(block({ confidence_min: 0.75 }));
  const slider = screen.getByLabelText(/act above confidence/i);
  fireEvent.change(slider, { target: { value: "0.6" } });
  fireEvent.pointerUp(slider);
  await waitFor(() =>
    expect(screen.getByText(/couldn’t save/i)).toBeInTheDocument(),
  );
});

test("the saved-toast timer does not outlive the panel (#922)", async () => {
  // The sibling of `PulseSettings`' regression, and it exists because the grep that found this
  // second copy is worth nothing if the second copy is not pinned. Same reasoning throughout:
  // the CI symptom needs the jsdom ENVIRONMENT torn down, which one case cannot do, so this
  // asserts the property the fix establishes — after unmount there is no pending timer.
  vi.useFakeTimers();
  try {
    const { unmount } = renderPanel(block({ stale_hours: 24 }));
    await act(async () => {
      fireEvent.change(screen.getByLabelText(/idle for/i), {
        target: { value: "6" },
      });
      await Promise.resolve();
    });
    // The precondition, asserted so this cannot quietly stop exercising the path it names.
    expect(vi.getTimerCount()).toBeGreaterThan(0);

    unmount();
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});

test("a save resolving AFTER unmount does not arm a new timer (#922 review 1)", async () => {
  // The sibling of PulseSettings' deferred-save regression. `saveGen` does not cover this:
  // it orders responses against each OTHER, and an unmount never bumps a generation.
  vi.useFakeTimers();
  try {
    let release!: (v: unknown) => void;
    vi.mocked(api.setPrefs).mockReturnValue(
      new Promise((r) => {
        release = r;
      }) as ReturnType<typeof api.setPrefs>,
    );

    const { unmount } = renderPanel(block({ stale_hours: 24 }));
    await act(async () => {
      fireEvent.change(screen.getByLabelText(/idle for/i), {
        target: { value: "6" },
      });
      await Promise.resolve();
    });
    expect(vi.getTimerCount()).toBe(0);

    unmount();
    await act(async () => {
      release({ orchestrator: block({ stale_hours: 6 }) });
      await Promise.resolve();
    });

    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(3000);
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});

test("the judgment threshold saves ONCE, on release, and cannot go under its floor (#1088)", async () => {
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockResolvedValue({ orchestrator: block({ judge_confidence_min: 0.95 }) });
  renderPanel(block({ judge_confidence_min: 0.9, judge_confidence_floor: 0.9, judge_confidence_max: 1 }));
  const slider = screen.getByTestId("judge-threshold") as HTMLInputElement;
  expect(slider.min).toBe("0.9");
  expect(slider.max).toBe("1");
  for (const v of ["0.91", "0.93", "0.95"]) fireEvent.change(slider, { target: { value: v } });
  expect(api.setPrefs).not.toHaveBeenCalled();
  fireEvent.pointerUp(slider);
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));
  expect(api.setPrefs).toHaveBeenCalledWith({ orchestrator: { judge_confidence_min: 0.95 } });
  const field = screen.getByTestId("orchestrator-judge");
  expect(field).toHaveTextContent(/0\.90 is the floor and cannot be lowered/);
  expect(field).toHaveTextContent(/at most move a mission to review/);
});
