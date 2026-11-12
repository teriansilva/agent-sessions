import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";

const api = vi.hoisted(() => ({ createReplyShare: vi.fn() }));
vi.mock("../../lib/api", async (orig) => ({ ...(await orig<typeof import("../../lib/api")>()), api }));

const { ReplyShareModal, REPLY_DISCLOSURE } = await import("./ReplyShareModal");
const { ApiError } = await import("../../lib/api");

const made = {
  share: {
    share_id: "Q3J9AAAAAAAAAAAAAAAAAA",
    session_key: "claude-api:x",
    turn_id: "t1",
    agent: "Claude",
    has_password: true,
    expires_at: null,
    created_at: 1,
    revoked_at: null,
    revoked_reason: null,
    open_count: 0,
    last_opened_at: null,
    live: true,
  },
  key: "S2V5S2V5S2V5",
  path: "/r/Q3J9AAAAAAAAAAAAAAAAAA",
};

function renderModal() {
  return render(
    <MemoryRouter>
      <ReplyShareModal sessionKey="claude-api:x" turnId="t1" agent="Claude" onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  api.createReplyShare.mockReset();
});

describe("ReplyShareModal (#1956)", () => {
  test("says what a visitor sees before anything is made", () => {
    renderModal();
    expect(screen.getByTestId("reply-share-disclosure")).toHaveTextContent(REPLY_DISCLOSURE);
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  });

  test("the link is this origin + /r/<id> with the key only in the fragment, shown once", async () => {
    api.createReplyShare.mockResolvedValue(made);
    renderModal();
    await userEvent.click(screen.getByRole("button", { name: "30 days" }));
    await userEvent.type(screen.getByLabelText(/Password/), "open sesame");
    await userEvent.click(screen.getByRole("button", { name: /Create link/ }));
    await waitFor(() =>
      expect(api.createReplyShare).toHaveBeenCalledWith({
        session: "claude-api:x",
        turn_id: "t1",
        expires_in: "30d",
        password: "open sesame",
      }),
    );
    const url = await screen.findByTestId("reply-share-url");
    expect(url).toHaveValue(`${window.location.origin}/r/Q3J9AAAAAAAAAAAAAAAAAA#k=S2V5S2V5S2V5`);
    expect(screen.getByTestId("reply-share-created")).toHaveTextContent("PASSWORD");
  });

  test("no password is sent when none is typed, and the server's refusal is said", async () => {
    api.createReplyShare.mockRejectedValue(new ApiError(422, "only a finished reply can be shared"));
    renderModal();
    await userEvent.click(screen.getByRole("button", { name: /Create link/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("only a finished reply can be shared");
    expect(api.createReplyShare.mock.calls[0][0]).not.toHaveProperty("password");
  });
});
