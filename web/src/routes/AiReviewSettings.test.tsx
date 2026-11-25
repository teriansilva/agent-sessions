import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AiReviewConfig, AppConfig, Session } from "../types/api";
import { AiReviewSettings } from "./AiReviewSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      setPrefs: vi.fn(),
      aiReviewModels: vi.fn(),
      sessions: vi.fn(),
      reviewExclude: vi.fn(),
    },
  };
});

function aiBlock(over: Partial<AiReviewConfig> = {}): AiReviewConfig {
  return {
    enabled: false,
    base_url: "https://ai.example.io/v1",
    model: "minimax-m2.7",
    interval_minutes: 5,
    max_input_chars: 24000,
    request_timeout: null,
    api_key_set: true,
    configured: true,
    ...over,
  };
}

function sess(id: string, title: string, over: Partial<Session> = {}): Session {
  return {
    id,
    engine: "claude",
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/x",
    project: "/home/m/x",
    last_mtime: 1000,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
    ...over,
  };
}

function renderPanel(
  block: AiReviewConfig | undefined = aiBlock(),
  refresh: () => void = () => {},
) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    ai_review: block,
  };
  // A router, because the prompt link is an in-app link to the Prompts page. The endpoint half
  // of this component moved to AiEndpointSetup in #956; its tests live beside it.
  return render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={refresh}>
        <ConfigCtx.Provider value={config as AppConfig}>
          <AiReviewSettings />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.setPrefs).mockImplementation(async (p) => ({
    ai_review: {
      ...aiBlock(),
      ...(p as { ai_review: object }).ai_review,
      api_key_set: true,
    },
  }));
  vi.mocked(api.aiReviewModels).mockResolvedValue({
    models: ["m-a", "m-b", "minimax-m2.7"],
  });
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [],
    next_offset: null,
    total: 0,
    facets: { projects: [], engines: [] },
  });
  vi.mocked(api.reviewExclude).mockResolvedValue({
    id: "claude:a",
    review_excluded: false,
  });
});

/** With a key on file the panel shows a static readout, not an input — a field that isn't
 *  on the page can't be autofilled (#834). Click "Replace key" to put one there. */

test("the review prompt is edited in the Prompts catalog, not here (#824)", async () => {
  renderPanel();
  // One editor per value: this panel owns the ENDPOINT, the catalog owns the prompts.
  expect(screen.queryByRole("textbox", { name: "Review prompt" })).toBeNull();
  const link = screen.getByRole("link", { name: /prompts → tail review/i });
  expect(link).toHaveAttribute("href", "/settings/ai-prompts#prompt-tail_review");
});

test("excluded sessions list re-includes a session", async () => {
  const user = userEvent.setup();
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [
      sess("claude:a", "rotate creds", { review_excluded: true }),
      sess("claude:b", "not excluded"),
    ],
    next_offset: null,
    total: 2,
    facets: { projects: [], engines: [] },
  });
  renderPanel();
  expect(await screen.findByText("rotate creds")).toBeInTheDocument();
  expect(screen.queryByText("not excluded")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Include" }));
  await waitFor(() =>
    expect(api.reviewExclude).toHaveBeenCalledWith("claude:a", false),
  );
  await waitFor(() =>
    expect(screen.queryByText("rotate creds")).not.toBeInTheDocument(),
  );
});

test("every successful save refreshes the config context, not only a `configured` flip (#956)", async () => {
  // One page per section makes a remount on navigation routine. A save that left the shared
  // context stale would show the pre-save value on the next visit — the #667 failure mode — so
  // the refresh no longer waits for `configured` to change.
  const user = userEvent.setup();
  const refresh = vi.fn();
  renderPanel(aiBlock(), refresh); // already configured; the echo stays configured
  await user.click(
    screen.getByRole("checkbox", { name: /enable periodic reviews/i }),
  );
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
});

test("the enable toggle persists immediately", async () => {
  const user = userEvent.setup();
  renderPanel();
  await user.click(
    screen.getByRole("checkbox", { name: /enable periodic reviews/i }),
  );
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { enabled: true } }),
  );
});

// Rejected saves on the Session review page are visible (Hermes on #957).

test("a rejected interval says why, and the rejected value stays visible for correction (#957)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValue(
    new ApiError(422, "ai_review.interval_minutes must be an integer between 1 and 1440"),
  );
  renderPanel();
  const interval = screen.getByLabelText("Review every");
  await user.clear(interval);
  await user.type(interval, "999999");
  await user.tab();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "ai_review.interval_minutes must be an integer between 1 and 1440",
  );
  expect(interval).toHaveValue(999999);
});

test("a rejected enable toggle says why and the checkbox keeps the saved state (#957)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("network down"));
  renderPanel(aiBlock({ enabled: false }));
  const toggle = screen.getByRole("checkbox", { name: /enable periodic reviews/i });
  await user.click(toggle);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t save — please try again.",
  );
  expect(toggle).not.toBeChecked();
});

test("a successful save says Saved. on this page", async () => {
  const user = userEvent.setup();
  renderPanel(aiBlock({ enabled: false }));
  await user.click(screen.getByRole("checkbox", { name: /enable periodic reviews/i }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
});
