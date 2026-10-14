import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

// The dialog holds the link's ONLY copy once the create answers (#1404 P4). Hermes on #1444:
// closing it while the create is in flight left a live link nobody could ever copy.
const api = vi.hoisted(() => ({ shares: vi.fn(), createShare: vi.fn(), revokeShare: vi.fn() }));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { SharePubliclyModal } = await import("./SharePubliclyModal");

const SHARE = {
  share_id: "s1",
  session_key: "codex-api:1",
  kind: "api",
  mode: "view",
  title: "T",
  has_password: false,
  expires_at: null,
  created_at: 1_800_000_000,
  revoked_at: null,
  revoked_reason: null,
  open_count: 0,
  last_opened_at: null,
};

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.shares.mockResolvedValue({ available: true, reason: null, shares: [] });
});

test("the dialog can't be dismissed while the create is in flight, then shows the link", async () => {
  let settle!: (v: unknown) => void;
  api.createShare.mockReturnValueOnce(new Promise((r) => (settle = r)));
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <SharePubliclyModal sessionKey="codex-api:1" title="T" agent="Codex — API" onClose={onClose} />
    </MemoryRouter>,
  );
  const create = await screen.findByRole("button", { name: /Create public link/ });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  await waitFor(() => expect(api.createShare).toHaveBeenCalledTimes(1));

  // Every way out is shut while the answer is pending.
  expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.mouseDown(screen.getByTestId("share-publicly").parentElement!);
  expect(onClose).not.toHaveBeenCalled();

  await act(async () => settle({ share: SHARE, url: "https://lab.example/p/tok" }));
  expect(await screen.findByTestId("share-url")).toHaveValue("https://lab.example/p/tok");
  // Once it is on screen: a stray click outside still doesn't lose it; Close does close.
  fireEvent.mouseDown(screen.getByTestId("share-publicly").parentElement!);
  expect(onClose).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a send link is created with the risk accepted; a view link never sends it", async () => {
  api.createShare.mockResolvedValue({ share: SHARE, url: "https://lab.example/p/tok" });
  render(
    <MemoryRouter>
      <SharePubliclyModal sessionKey="codex-api:1" title="T" agent="Codex — API" onClose={vi.fn()} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("radio", { name: /Can send messages/ }));
  expect(screen.getByTestId("share-risk")).toHaveTextContent("saved secret values the agent can see");
  const create = screen.getByRole("button", { name: /Create public link/ });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  await waitFor(() => expect(api.createShare).toHaveBeenCalledTimes(1));
  expect(api.createShare.mock.calls[0][0]).toMatchObject({ mode: "send", accept_risk: true });
});
