import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AppConfig, SessionReviewConfig } from "../types/api";
import { SessionReviewDepth } from "./AiReviewSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

function renderDepth(block?: SessionReviewConfig, refresh: () => void = () => {}) {
  const config = { csrf: "t", session_review: block } as unknown as AppConfig;
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <SessionReviewDepth />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.setPrefs).mockImplementation(async (p) => ({
    session_review: {
      recognise_prompts: true,
      decision_context: "standard",
      ...(p as { session_review: object }).session_review,
    },
  }));
});

test("reflects the stored block (#1086 P2)", () => {
  renderDepth({ recognise_prompts: false, decision_context: "deep" });
  expect(screen.getByLabelText(/recognise questions and choices/i)).not.toBeChecked();
  expect(screen.getByLabelText("Deep")).toBeChecked();
  expect(screen.getByText(/reads the end of the conversation/i)).toBeInTheDocument();
});

test("saves ONLY the session_review block and refreshes the shared config", async () => {
  const refresh = vi.fn();
  renderDepth({ recognise_prompts: true, decision_context: "standard" }, refresh);
  await userEvent.click(screen.getByLabelText("Deep"));
  expect(api.setPrefs).toHaveBeenCalledWith({ session_review: { decision_context: "deep" } });
  await userEvent.click(screen.getByLabelText(/recognise questions and choices/i));
  expect(api.setPrefs).toHaveBeenLastCalledWith({
    session_review: { recognise_prompts: false },
  });
  // Never through the ai_review block, which carries the endpoint key.
  for (const [arg] of vi.mocked(api.setPrefs).mock.calls) {
    expect(Object.keys(arg as object)).toEqual(["session_review"]);
  }
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(2));
});

test("a refused save restores the stored value and says why", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new ApiError(422, "nope"));
  renderDepth({ recognise_prompts: true, decision_context: "standard" });
  await userEvent.click(screen.getByLabelText("Deep"));
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  expect(screen.getByLabelText("Standard")).toBeChecked();
});

test("with no stored block it shows the server defaults", () => {
  renderDepth(undefined);
  expect(screen.getByLabelText(/recognise questions and choices/i)).toBeChecked();
  expect(screen.getByLabelText("Standard")).toBeChecked();
});

test("ONE save at a time: a second change is refused while the first is in flight (review 5180)", async () => {
  // The reproduction: hold the Deep save pending, change recognition, then REJECT the first.
  // Overlapping saves let the older failure restore its snapshot over the newer success; now the
  // controls are disabled while a save is in flight, so there is no second request to overtake.
  let reject!: (e: unknown) => void;
  vi.mocked(api.setPrefs).mockImplementationOnce(
    () => new Promise((_resolve, rej) => { reject = rej; }),
  );
  const refresh = vi.fn();
  renderDepth({ recognise_prompts: true, decision_context: "standard" }, refresh);
  await userEvent.click(screen.getByLabelText("Deep"));
  const recognise = screen.getByLabelText(/recognise questions and choices/i);
  expect(recognise).toBeDisabled();
  expect(screen.getByLabelText("Standard")).toBeDisabled();
  await userEvent.click(recognise); // ignored: disabled
  expect(api.setPrefs).toHaveBeenCalledTimes(1);

  reject(new ApiError(500, "boom"));
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  // The failure shows the last known value AND re-reads the server's authoritative block.
  expect(screen.getByLabelText("Standard")).toBeChecked();
  expect(recognise).toBeChecked();
  expect(recognise).not.toBeDisabled();
  expect(refresh).toHaveBeenCalledTimes(1);
});

test("the server's refreshed block wins after a failed save", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new ApiError(500, "boom"));
  const view = renderDepth({ recognise_prompts: true, decision_context: "standard" });
  await userEvent.click(screen.getByLabelText("Deep"));
  await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  // What the refresh brings back (say another tab saved Deep meanwhile) is what shows.
  const config = { csrf: "t", session_review: { recognise_prompts: false, decision_context: "deep" } };
  view.rerender(
    <ConfigRefreshCtx.Provider value={() => {}}>
      <ConfigCtx.Provider value={config as unknown as AppConfig}>
        <SessionReviewDepth />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
  expect(screen.getByLabelText("Deep")).toBeChecked();
  expect(screen.getByLabelText(/recognise questions and choices/i)).not.toBeChecked();
});
