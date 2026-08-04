/** The idle window is an operator setting (#768).
 *
 * It replaced a hard-coded 48h constant. Measured on a live store, the median session was
 * 30.4h idle when the orchestrator escalated it — so 48h removed 18% of the notification
 * volume where the 24h default removes 52%. What the panel has to get right is that the
 * chosen value actually reaches the server, and that remounting shows the saved value rather
 * than the pre-save one (the #667 stale-ConfigCtx failure).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    prompt: "p",
    notify: "escalations",
    configured: true,
    default_prompt: "p",
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
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <OrchestratorSettings />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
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
    screen.getByText(/stays on your Pulse cards and in the sidebar/i),
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

  await waitFor(() => expect(screen.getByText("0.90")).toBeInTheDocument());
  // …now the older response finally lands, carrying the older value.
  releaseFirst({ orchestrator: block({ confidence_min: 0.6 }) });
  await new Promise((r) => setTimeout(r, 0));
  expect(screen.getByText("0.90")).toBeInTheDocument();
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

  await waitFor(() => expect(screen.getByText("0.90")).toBeInTheDocument());

  rejectFirst(new Error("network died"));
  await new Promise((r) => setTimeout(r, 0));

  expect(screen.queryByText(/couldn’t save/i)).toBeNull();
  expect(screen.getByText("0.90")).toBeInTheDocument();
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
