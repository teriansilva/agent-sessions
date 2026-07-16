import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { HandoffModal } from "./HandoffModal";

// Hand-off modal (#597, Phase 1): tiles come from /api/engines' supports_seed_start (the
// server-shared capability source), prepare backs the preview, commit navigates to the
// fresh-launch route. jsdom covers the component logic; the real-browser interaction is
// pinned by web/e2e/handoff.spec.ts.

vi.mock("../../lib/api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../../lib/api")>();
  return {
    ApiError: orig.ApiError,
    api: {
      engines: vi.fn(),
      prepareHandoff: vi.fn(),
      commitHandoff: vi.fn(),
    },
  };
});

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const orig = await importOriginal<typeof import("react-router-dom")>();
  return { ...orig, useNavigate: () => mockNavigate };
});

const ENGINES = {
  engines: [
    { id: "claude", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/claude" },
    { id: "codex", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/codex" },
    { id: "gemini", present: true, supports_new: true, supports_seed_start: false, seed_reason: "no seed-capable start yet", bin: "/bin/gemini" },
    { id: "shell", present: true, supports_new: true, supports_seed_start: false, seed_reason: "not an agent engine", bin: "/bin/bash" },
  ],
};

const PREPARED = {
  handle: "h-1",
  preview: "# Handoff — continued from a claude session\n[user] do the thing",
  meta: { mode: "quick", turns: 2, bytes: 60, cap: 8192 },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.engines).mockResolvedValue(ENGINES as never);
  vi.mocked(api.prepareHandoff).mockResolvedValue(PREPARED as never);
});

function renderModal() {
  return render(
    <MemoryRouter>
      <HandoffModal
        sessionId="claude:11111111-1111-1111-1111-111111111111"
        engine="claude"
        title="Fix the auth race"
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
}

test("renders capability-driven tiles, defaults to a non-source engine, shows the preview", async () => {
  renderModal();
  // shell is never offered; gemini renders disabled with its server-supplied reason.
  const codex = await screen.findByRole("radio", { name: /codex/i });
  await waitFor(() => expect(codex).toHaveAttribute("aria-checked", "true"));
  expect(screen.queryByRole("radio", { name: /shell/i })).toBeNull();
  const gemini = screen.getByRole("radio", { name: /gemini/i });
  expect(gemini).toBeDisabled();
  expect(gemini.textContent).toMatch(/no seed-capable start yet/i);
  // The prepared seed backs the read-only preview; AI summary is visibly Phase 2.
  expect(api.prepareHandoff).toHaveBeenCalledWith(
    "claude:11111111-1111-1111-1111-111111111111",
    "codex",
  );
  const preview = await screen.findByLabelText(/seed preview/i);
  expect(preview).toHaveValue(PREPARED.preview);
  expect(preview).toHaveAttribute("readonly");
  expect(screen.getByRole("button", { name: /ai summary/i })).toBeDisabled();
});

test("switching tiles re-prepares against the new target", async () => {
  renderModal();
  const claude = await screen.findByRole("radio", { name: /claude/i });
  await userEvent.click(claude); // same-engine handoff is allowed
  await waitFor(() =>
    expect(api.prepareHandoff).toHaveBeenLastCalledWith(
      "claude:11111111-1111-1111-1111-111111111111",
      "claude",
    ),
  );
});

test("confirm commits the handle and navigates to the fresh-launch route", async () => {
  vi.mocked(api.commitHandoff).mockResolvedValue({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  } as never);
  renderModal();
  const go = await screen.findByRole("button", { name: /hand off session|^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  expect(api.commitHandoff).toHaveBeenCalledWith("h-1");
  expect(mockNavigate).toHaveBeenCalledWith("/s/codex/new-9", {
    state: { fresh: { cwd: "/repo", bypass: true } },
  });
});

test("prepare failure surfaces the server detail (empty transcript case)", async () => {
  vi.mocked(api.prepareHandoff).mockRejectedValue(
    new ApiError(409, "source transcript is empty — nothing to hand off"),
  );
  renderModal();
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/transcript is empty/i);
  const go = screen.getByRole("button", { name: /^hand off$/i });
  expect(go).toBeDisabled();
});
