import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
    {
      id: "claude",
      present: true,
      supports_new: true,
      supports_seed_start: true,
      seed_reason: null,
      bin: "/bin/claude",
    },
    {
      id: "codex",
      present: true,
      supports_new: true,
      supports_seed_start: true,
      seed_reason: null,
      bin: "/bin/codex",
    },
    {
      id: "gemini",
      present: true,
      supports_new: true,
      supports_seed_start: false,
      seed_reason: "no seed-capable start yet",
      bin: "/bin/gemini",
    },
    {
      id: "shell",
      present: true,
      supports_new: true,
      supports_seed_start: false,
      seed_reason: "not an agent engine",
      bin: "/bin/bash",
    },
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
  // The prepared seed backs the editable preview; both modes are selectable (Phase 2).
  expect(api.prepareHandoff).toHaveBeenCalledWith(
    "claude:11111111-1111-1111-1111-111111111111",
    "codex",
    "quick",
    false,
  );
  const preview = await screen.findByLabelText(/seed preview/i);
  expect(preview).toHaveValue(PREPARED.preview);
  expect(preview).not.toHaveAttribute("readonly");
  expect(screen.getByRole("radio", { name: /ai summary/i })).toBeEnabled();
});

test("switching tiles re-prepares against the new target", async () => {
  renderModal();
  const claude = await screen.findByRole("radio", { name: /claude/i });
  await userEvent.click(claude); // same-engine handoff is allowed
  await waitFor(() =>
    expect(api.prepareHandoff).toHaveBeenLastCalledWith(
      "claude:11111111-1111-1111-1111-111111111111",
      "claude",
      "quick",
      false,
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
  const go = await screen.findByRole("button", {
    name: /hand off session|^hand off$/i,
  });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  expect(api.commitHandoff).toHaveBeenCalledWith("h-1", undefined);
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

// --- Phase 2 ---------------------------------------------------------------------------

test("AI mode re-prepares in ai mode and surfaces a degrade notice", async () => {
  vi.mocked(api.prepareHandoff).mockResolvedValue({
    ...PREPARED,
    preview: "# Handoff\n## State\nlocal tail instead",
    meta: {
      mode: "quick",
      turns: 2,
      bytes: 30,
      cap: 8192,
      requested_mode: "ai",
      degraded: true,
      notice: "AI review isn't configured — using the local quick tail.",
    },
  } as never);
  renderModal();
  await userEvent.click(
    await screen.findByRole("radio", { name: /ai summary/i }),
  );
  await waitFor(() =>
    expect(api.prepareHandoff).toHaveBeenLastCalledWith(
      "claude:11111111-1111-1111-1111-111111111111",
      "codex",
      "ai",
      false,
    ),
  );
  // The server degraded to quick — the modal says so rather than pretending it's an AI brief.
  expect(
    await screen.findByText(/isn't configured — using the local quick tail/i),
  ).toBeVisible();
});

test("an edited preview is what gets committed", async () => {
  vi.mocked(api.commitHandoff).mockResolvedValue({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  } as never);
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "hand-written brief");
  await userEvent.click(screen.getByRole("button", { name: /^hand off$/i }));
  expect(api.commitHandoff).toHaveBeenCalledWith("h-1", "hand-written brief");
});

test("switching target asks before discarding a dirty edit, and honours both answers", async () => {
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "edit for codex");
  // #703 review: a switch must not silently throw away typed prose.
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  expect(
    await screen.findByRole("alertdialog", { name: /discard your edits/i }),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: /keep editing/i }));
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue("edit for codex");
  expect(screen.getByRole("radio", { name: /codex/i })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  // Confirming does switch, and the edit goes with the seed it belonged to.
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  await userEvent.click(
    await screen.findByRole("button", { name: /discard & rebuild/i }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
      PREPARED.preview,
    ),
  );
});

test("a clean preview switches target with no confirmation", async () => {
  renderModal();
  await screen.findByLabelText(/seed preview/i);
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  await waitFor(() =>
    expect(api.prepareHandoff).toHaveBeenLastCalledWith(
      "claude:11111111-1111-1111-1111-111111111111",
      "claude",
      "quick",
      false,
    ),
  );
});

test("an expired handle re-prepares in place instead of dead-ending", async () => {
  vi.mocked(api.commitHandoff).mockRejectedValue(
    new ApiError(404, "unknown or expired handoff handle"),
  );
  renderModal();
  const go = await screen.findByRole("button", { name: /^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  // The "re-prepared" line is an info NOTICE (role=status), not an alert — so a failed
  // re-prepare can surface its own error instead of being masked (#703 review follow-up).
  expect(
    await screen.findByText(/a fresh seed is being prepared/i),
  ).toBeVisible();
  // A fresh prepare ran (nonce bump), so the retry can't reuse the dead handle.
  await waitFor(() => expect(api.prepareHandoff).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: /^hand off$/i })).toBeEnabled();
});

test("renewing an expired handle PRESERVES the user's edited brief (#703 review)", async () => {
  vi.mocked(api.commitHandoff).mockRejectedValueOnce(
    new ApiError(404, "unknown or expired handoff handle"),
  );
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "brief I typed by hand");
  await userEvent.click(screen.getByRole("button", { name: /^hand off$/i }));
  await screen.findByText(/a fresh seed is being prepared/i);
  await waitFor(() => expect(api.prepareHandoff).toHaveBeenCalledTimes(2));
  // The renewal rebuilt the handle, NOT the user's prose.
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
    "brief I typed by hand",
  );
  // …and the retry commits that same text against the fresh handle.
  vi.mocked(api.commitHandoff).mockResolvedValue({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  } as never);
  await userEvent.click(screen.getByRole("button", { name: /^hand off$/i }));
  expect(api.commitHandoff).toHaveBeenLastCalledWith(
    "h-1",
    "brief I typed by hand",
  );
});

test("an over-cap edit is blocked client-side against the server's meta.cap (#703 review)", async () => {
  vi.mocked(api.prepareHandoff).mockResolvedValue({
    ...PREPARED,
    meta: { ...PREPARED.meta, cap: 64 },
  } as never);
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "z".repeat(120));
  // The CTA is gated on the SAME cap the server enforces — no silent truncation, and no
  // inviting an edit that commit would reject.
  expect(screen.getByRole("button", { name: /^hand off$/i })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent(/over the .* KB limit/i);
  expect(api.commitHandoff).not.toHaveBeenCalled();
});

test("every dismissal path is locked while a commit is in flight", async () => {
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.commitHandoff).mockReturnValue(
    new Promise((r) => {
      release = r;
    }) as never,
  );
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <HandoffModal
        sessionId="claude:11111111-1111-1111-1111-111111111111"
        engine="claude"
        title="Fix the auth race"
        onClose={onClose}
      />
    </MemoryRouter>,
  );
  const go = await screen.findByRole("button", { name: /^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  // Commit pending: cancel/close/tiles are disabled and Escape is inert, so a late
  // response can't redirect a user who thought they'd left.
  expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
  expect(
    screen.getByRole("button", { name: /close hand-off/i }),
  ).toBeDisabled();
  expect(screen.getByRole("radio", { name: /codex/i })).toBeDisabled();
  await userEvent.keyboard("{Escape}");
  expect(onClose).not.toHaveBeenCalled();
  release({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  });
});

test("a pending discard decision suspends the handoff action (#703 review r3)", async () => {
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "half-written brief");
  // Open the discard confirmation by asking to switch target…
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  await screen.findByRole("alertdialog", { name: /discard your edits/i });
  // …while it's open, Hand off must NOT commit the handle/target being abandoned.
  const go = screen.getByRole("button", { name: /^hand off$/i });
  expect(go).toBeDisabled();
  await userEvent.click(go);
  expect(api.commitHandoff).not.toHaveBeenCalled();
  // Resolving the decision restores the action.
  await userEvent.click(screen.getByRole("button", { name: /keep editing/i }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: /^hand off$/i })).toBeEnabled(),
  );
});

test("the discard confirmation locks while a commit is in flight (#703 review r3)", async () => {
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.commitHandoff).mockReturnValue(
    new Promise((r) => {
      release = r;
    }) as never,
  );
  renderModal();
  const go = await screen.findByRole("button", { name: /^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go); // commit in flight
  // A switch requested mid-commit can't rebuild the modal underneath the navigation.
  expect(screen.getByRole("radio", { name: /claude/i })).toBeDisabled();
  release({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  });
});

test("a switch DURING an expired-handle renewal still guards the dirty edit (#703 review r4)", async () => {
  // Reproduction: edit → commit 404s → the 2nd prepare is held pending (prep === null) →
  // switch target. The discard guard must still fire even though prep is momentarily null.
  let releasePrepare: (v: unknown) => void = () => {};
  vi.mocked(api.commitHandoff).mockRejectedValueOnce(
    new ApiError(404, "unknown or expired handoff handle"),
  );
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "authoritative brief");
  // Hold the renewal's prepare pending so prep stays null through the switch.
  vi.mocked(api.prepareHandoff).mockImplementationOnce(
    () =>
      new Promise((res) => {
        releasePrepare = res;
      }) as never,
  );
  await userEvent.click(screen.getByRole("button", { name: /^hand off$/i }));
  await screen.findByText(/a fresh seed is being prepared/i);
  // The 2nd prepare is in flight (prep === null). Switching target must NOT silently drop
  // the brief — the confirmation appears.
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  expect(
    await screen.findByRole("alertdialog", { name: /discard your edits/i }),
  ).toBeVisible();
  // Keeping the edit preserves it and the renewal's target.
  await userEvent.click(screen.getByRole("button", { name: /keep editing/i }));
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
    "authoritative brief",
  );
  releasePrepare({
    handle: "h-renewed",
    preview: PREPARED.preview,
    meta: PREPARED.meta,
  });
});

test("a FAILED expired-handle renewal surfaces the real error and offers retry (#703 review follow-up)", async () => {
  // Commit 404 → re-prepare, but the re-prepare itself fails. The authoritative prepare
  // error must show (not be masked by the reassuring 'fresh seed' notice), with a retry.
  vi.mocked(api.commitHandoff).mockRejectedValueOnce(
    new ApiError(404, "unknown or expired handoff handle"),
  );
  // First prepare (initial) succeeds; the renewal prepare (2nd call) fails hard.
  vi.mocked(api.prepareHandoff)
    .mockResolvedValueOnce(PREPARED as never)
    .mockRejectedValueOnce(
      new ApiError(422, "target engine unavailable: not installed"),
    );
  renderModal();
  const go = await screen.findByRole("button", { name: /^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go);
  // The real prepare failure shows — NOT the "fresh seed is being prepared" notice.
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/target engine unavailable/i);
  expect(screen.queryByText(/a fresh seed is being prepared/i)).toBeNull();
  // …and a Retry recovers the same selection.
  vi.mocked(api.prepareHandoff).mockResolvedValueOnce(PREPARED as never);
  await userEvent.click(within(alert).getByRole("button", { name: /retry/i }));
  await waitFor(() =>
    expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
      PREPARED.preview,
    ),
  );
  expect(screen.getByRole("button", { name: /^hand off$/i })).toBeEnabled();
});

test("an initial prepare failure offers a retry (#703 review follow-up)", async () => {
  vi.mocked(api.prepareHandoff).mockRejectedValueOnce(
    new ApiError(502, "endpoint blew up"),
  );
  renderModal();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/endpoint blew up/i);
  vi.mocked(api.prepareHandoff).mockResolvedValueOnce(PREPARED as never);
  await userEvent.click(within(alert).getByRole("button", { name: /retry/i }));
  await waitFor(() =>
    expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
      PREPARED.preview,
    ),
  );
});

test("Escape cancels the discard-confirm first, not the parent modal (#703 review 2586)", async () => {
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <HandoffModal
        sessionId="claude:11111111-1111-1111-1111-111111111111"
        engine="claude"
        title="Fix the auth race"
        onClose={onClose}
      />
    </MemoryRouter>,
  );
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "precious edit");
  await userEvent.click(screen.getByRole("radio", { name: /claude/i })); // opens the confirm
  const confirm = await screen.findByRole("alertdialog", {
    name: /discard your edits/i,
  });
  // Focus moved into the confirm (onto the safe default).
  expect(
    within(confirm).getByRole("button", { name: /keep editing/i }),
  ).toHaveFocus();
  // Escape cancels the CONFIRM, not the whole modal — the edit is preserved.
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue("precious edit");
});

test("a backdrop click cancels the discard-confirm first, not the parent (#703 review 2586)", async () => {
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <HandoffModal
        sessionId="claude:11111111-1111-1111-1111-111111111111"
        engine="claude"
        title="Fix the auth race"
        onClose={onClose}
      />
    </MemoryRouter>,
  );
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "still here");
  await userEvent.click(screen.getByRole("radio", { name: /claude/i }));
  const dialog = await screen.findByRole("dialog");
  await screen.findByRole("alertdialog");
  // The backdrop is the dialog's parent; a mousedown on it (outside the dialog) dismisses.
  fireEvent.mouseDown(dialog.parentElement as Element);
  expect(screen.queryByRole("alertdialog")).toBeNull(); // confirm cancelled
  expect(onClose).not.toHaveBeenCalled(); // parent NOT closed
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue("still here");
});

test("a commit that resolves after the modal unmounts does not navigate (#703 review 2586)", async () => {
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.commitHandoff).mockReturnValue(
    new Promise((r) => {
      release = r;
    }) as never,
  );
  const { unmount } = render(
    <MemoryRouter>
      <HandoffModal
        sessionId="claude:11111111-1111-1111-1111-111111111111"
        engine="claude"
        title="Fix the auth race"
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  const go = await screen.findByRole("button", { name: /^hand off$/i });
  await waitFor(() => expect(go).toBeEnabled());
  await userEvent.click(go); // commit in flight
  unmount(); // browser Back / external route change tears the modal down
  release({
    id: "codex:new-9",
    engine: "codex",
    native: "new-9",
    cwd: "/repo",
  });
  await Promise.resolve();
  // The stale continuation must NOT override the user's newer location.
  expect(mockNavigate).not.toHaveBeenCalled();
});

// --- source reference (#716) ---------------------------------------------------------------
// The transcript locator is opt-in. Because it changes the GENERATED seed, the flag has to key
// the prepared result and the edit exactly like target/mode do — otherwise a toggle can leave a
// stale handle on screen or silently discard typed prose.

test("toggling the source reference re-prepares with the flag set", async () => {
  renderModal();
  await screen.findByLabelText(/seed preview/i);
  const opt = screen.getByRole("checkbox", {
    name: /reference the source session/i,
  });
  expect(opt).not.toBeChecked(); // opt-in: off by default
  await userEvent.click(opt);
  await waitFor(() =>
    expect(api.prepareHandoff).toHaveBeenLastCalledWith(
      "claude:11111111-1111-1111-1111-111111111111",
      "codex",
      "quick",
      true,
    ),
  );
  expect(opt).toBeChecked();
});

test("the extra privacy disclosure appears only while the source reference is on", async () => {
  renderModal();
  await screen.findByLabelText(/seed preview/i);
  expect(screen.queryByText(/a local path that can reveal/i)).toBeNull();
  await userEvent.click(
    screen.getByRole("checkbox", { name: /reference the source session/i }),
  );
  expect(
    await screen.findByText(/a local path that can reveal/i),
  ).toBeVisible();
  expect(
    screen.getByText(/no transcript contents are included/i),
  ).toBeVisible();
});

test("a late prepare for the previous flag value cannot overwrite the current preview", async () => {
  // Rapid toggle: the OFF request resolves after the ON one. Keyed results mean the stale
  // response is dropped rather than repainting the preview with the wrong seed.
  let resolveOff!: (v: unknown) => void;
  vi.mocked(api.prepareHandoff).mockImplementation((_s, _t, _m, ref) =>
    ref
      ? (Promise.resolve({
          ...PREPARED,
          handle: "h-on",
          preview: "WITH LOCATOR",
        }) as never)
      : (new Promise((res) => {
          resolveOff = res;
        }) as never),
  );
  renderModal();
  const opt = await screen.findByRole("checkbox", {
    name: /reference the source session/i,
  });
  await userEvent.click(opt); // → ON, resolves immediately
  await waitFor(() =>
    expect(screen.getByLabelText(/seed preview/i)).toHaveValue("WITH LOCATOR"),
  );
  // The earlier OFF request lands late; it belongs to a key nobody is showing any more.
  resolveOff({ ...PREPARED, handle: "h-off", preview: "STALE NO LOCATOR" });
  await waitFor(() =>
    expect(screen.getByLabelText(/seed preview/i)).toHaveValue("WITH LOCATOR"),
  );
});

test("toggling the source reference asks before discarding a dirty edit", async () => {
  renderModal();
  const preview = await screen.findByLabelText(/seed preview/i);
  await userEvent.clear(preview);
  await userEvent.type(preview, "hand-written brief");
  await userEvent.click(
    screen.getByRole("checkbox", { name: /reference the source session/i }),
  );
  expect(
    await screen.findByRole("alertdialog", { name: /discard your edits/i }),
  ).toBeVisible();
  // Keeping the edit also keeps the toggle where it was — the switch never happened.
  await userEvent.click(screen.getByRole("button", { name: /keep editing/i }));
  expect(screen.getByLabelText(/seed preview/i)).toHaveValue(
    "hand-written brief",
  );
  expect(
    screen.getByRole("checkbox", { name: /reference the source session/i }),
  ).not.toBeChecked();
});
