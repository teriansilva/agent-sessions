import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { PublicSnapshot, Unlocked, VisitorShare } from "./publicApi";

// The viewer's client-side rules (#1404 P4, the #1444 independent review): the retry key of a
// send, the name gate, re-unlocking without a loop, and following another tab's re-unlock.
const api = vi.hoisted(() => ({
  unlock: vi.fn(),
  share: vi.fn(),
  setName: vi.fn(),
  session: vi.fn(),
  events: vi.fn(),
  turn: vi.fn(),
  interrupt: vi.fn(),
  sendNow: vi.fn(),
  upload: vi.fn(),
}));
vi.mock("./publicApi", async (orig) => ({
  ...(await orig<typeof import("./publicApi")>()),
  publicApi: api,
}));

const { PublicError } = await import("./publicApi");
const { Composer } = await import("./Composer");
const { ApiView } = await import("./ApiView");
const { ShareViewer } = await import("./ShareViewer");

const TOKEN = "A".repeat(43);

function share(over: Partial<VisitorShare> = {}): VisitorShare {
  return {
    link: "LINK-A",
    kind: "api",
    mode: "send",
    title: "Fix the auth redirect loop",
    expires_at: null,
    name: "Alex",
    name_required: false,
    ...over,
  };
}

function snap(over: Partial<PublicSnapshot> = {}): PublicSnapshot {
  return {
    link: "LINK-A",
    revision: 1,
    event_cursor: 1,
    state: "idle",
    active_turn: null,
    model: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    read_only: false,
    images: false,
    can_send: true,
    mission_held: false,
    ...over,
  };
}

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
});
afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("Composer", () => {
  function renderComposer(over: Partial<VisitorShare> = {}) {
    const onShare = vi.fn();
    const onRecheck = vi.fn(async () => {});
    const onSent = vi.fn(async () => {});
    render(
      <Composer
        share={share(over)}
        csrf="c1"
        snap={snap()}
        onShare={onShare}
        onSent={onSent}
        onRecheck={onRecheck}
        refused={() => false}
      />,
    );
    return { onShare, onRecheck, onSent };
  }

  test("a send that may have landed keeps its retry key; a refusal gets a new one", async () => {
    renderComposer();
    const box = screen.getByLabelText("Message the agent");
    await userEvent.type(box, "check the logout path");
    api.turn.mockRejectedValueOnce(new PublicError(503, "try again"));
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByRole("alert");
    api.turn.mockRejectedValueOnce(new PublicError(0, "offline"));
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.turn).toHaveBeenCalledTimes(2));
    const [first, second] = api.turn.mock.calls.map((c) => c[1].client_key);
    expect(second).toBe(first); // the server answers the recorded turn, never a second one
    api.turn.mockRejectedValueOnce(new PublicError(422, "that message is too long"));
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.turn).toHaveBeenCalledTimes(3));
    api.turn.mockResolvedValueOnce({ state: "running" });
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.turn).toHaveBeenCalledTimes(4));
    expect(api.turn.mock.calls[3][1].client_key).not.toBe(first); // after a 4xx: a fresh key
    expect(api.turn.mock.calls[3][0]).toBe("c1"); // the share CSRF token
    await waitFor(() => expect(box).toHaveValue(""));
  });

  test("a failed re-read after a send is not a failed send", async () => {
    const { onSent } = renderComposer();
    api.turn.mockResolvedValueOnce({ state: "running" });
    await userEvent.type(screen.getByLabelText("Message the agent"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onSent).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText("Message the agent")).toHaveValue("");
  });

  test("'enter your name first' brings the name form back; 'bad csrf' re-reads the link", async () => {
    const { onShare, onRecheck } = renderComposer();
    await userEvent.type(screen.getByLabelText("Message the agent"), "hi");
    api.turn.mockRejectedValueOnce(new PublicError(403, "enter your name first"));
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onShare).toHaveBeenCalledWith(expect.objectContaining({ name_required: true })));
    api.turn.mockRejectedValueOnce(new PublicError(403, "bad csrf"));
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(onRecheck).toHaveBeenCalled());
    expect(await screen.findByRole("alert")).toHaveTextContent("opened again in another tab");
  });

  test("a nameless send visitor gets the name form, not the composer", () => {
    renderComposer({ name: null, name_required: true });
    expect(screen.getByLabelText("Your name — shown with your messages")).toBeInTheDocument();
    expect(screen.queryByLabelText("Message the agent")).toBeNull();
  });
});

describe("ShareViewer", () => {
  const unlocked: Unlocked = { share: share({ mode: "view", name: null }), csrf: "c1" };

  test("a browser that drops the cookie gets an explanation, not an unlock loop", async () => {
    window.history.replaceState(null, "", `/p/${TOKEN}`);
    api.unlock.mockResolvedValue(unlocked);
    api.session.mockRejectedValue(new PublicError(401, "unlock this link first"));
    render(<ShareViewer />);
    expect(await screen.findByTestId("share-nocookie")).toBeInTheDocument();
    expect(api.unlock).toHaveBeenCalledTimes(1);
    expect(window.location.pathname).toBe("/p/"); // the token left the address bar
  });

  test("a mangled link is unavailable, never 'open it again'", async () => {
    window.history.replaceState(null, "", `/p/${TOKEN}).`);
    render(<ShareViewer />);
    expect(await screen.findByTestId("share-unavailable")).toBeInTheDocument();
    expect(api.unlock).not.toHaveBeenCalled();
    expect(api.share).not.toHaveBeenCalled();
  });

  test("a reload on /p/ reads the link through the cookie; 401 there asks for the link", async () => {
    window.history.replaceState(null, "", "/p/");
    api.share.mockRejectedValue(new PublicError(401, "unlock this link first"));
    render(<ShareViewer />);
    expect(await screen.findByTestId("share-relink")).toBeInTheDocument();
    expect(api.unlock).not.toHaveBeenCalled();
  });

  test("any 404 is the one unavailable state", async () => {
    window.history.replaceState(null, "", `/p/${TOKEN}`);
    api.unlock.mockResolvedValue(unlocked);
    api.session.mockResolvedValue(snap());
    api.events.mockRejectedValue(new PublicError(404, "link unavailable"));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ShareViewer />);
      await screen.findByTestId("share-transcript");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(11_000);
      });
      expect(await screen.findByTestId("share-unavailable")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

function queued(over: Record<string, unknown>) {
  return {
    turn_id: "q1",
    state: "queued",
    delivery: null,
    text: "my queued message",
    text_truncated: false,
    reply: null,
    reply_ts: null,
    reply_truncated: false,
    attachments: [],
    tools: [],
    tools_truncated: false,
    author: { name: "Alex", via: "public_link" as const },
    mine: false,
    ...over,
  };
}

describe("Hermes on #1444 (8bd27907)", () => {
  test("a send keeps words typed while it was in flight", async () => {
    let settle!: (v: unknown) => void;
    api.turn.mockReturnValueOnce(new Promise((r) => (settle = r)));
    render(
      <Composer
        share={share()}
        csrf="c1"
        snap={snap()}
        onShare={vi.fn()}
        onSent={vi.fn(async () => {})}
        onRecheck={vi.fn(async () => {})}
        refused={() => false}
      />,
    );
    const box = screen.getByLabelText("Message the agent");
    await userEvent.type(box, "first");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.turn).toHaveBeenCalledTimes(1));
    await userEvent.type(box, " and a second thought");
    await act(async () => settle({ state: "running" }));
    await waitFor(() => expect(box).toHaveValue("and a second thought"));
    expect(api.turn.mock.calls[0][1].text).toBe("first");
  });

  test("Send now follows the link the turn came through, never the name", async () => {
    const view = (name: string, mine: boolean) => {
      api.session.mockResolvedValue(
        snap({ active_turn: "t0", state: "running", turns: [queued({ mine })] }),
      );
      api.events.mockResolvedValue({ link: "LINK-A", revision: 1, next_cursor: 1 });
      return render(
        <ApiView
          unlocked={{ share: share({ name }), csrf: "c1" }}
          onShare={vi.fn()}
          onGone={vi.fn()}
          onExpired={vi.fn()}
          onRecheck={vi.fn(async () => {})}
        />,
      );
    };
    // Renamed on the same link: sent as Alex, now Bo — still theirs.
    const a = view("Bo", true);
    expect(await screen.findByText("my queued message")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send now" })).toBeInTheDocument();
    a.unmount();
    // The same name on another link: not theirs, no button.
    view("Alex", false);
    expect(await screen.findByText("my queued message")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send now" })).toBeNull();
  });

  test("Send now is offered only on the queue head, and an agent's own turn renders", async () => {
    api.session.mockResolvedValue(
      snap({
        active_turn: "t0",
        state: "running",
        turns: [
          { kind: "agent", turn_id: "g1", state: "completed", reply: "agent did this on its own", reply_ts: null, reply_truncated: false, tools: [], tools_truncated: false },
          queued({ turn_id: "q1", text: "first queued", mine: true }),
          queued({ turn_id: "q2", text: "second queued", mine: true }),
        ],
      }),
    );
    api.events.mockResolvedValue({ link: "LINK-A", revision: 1, next_cursor: 1 });
    render(
      <ApiView
        unlocked={{ share: share(), csrf: "c1" }}
        onShare={vi.fn()}
        onGone={vi.fn()}
        onExpired={vi.fn()}
        onRecheck={vi.fn(async () => {})}
      />,
    );
    expect(await screen.findByText("agent did this on its own")).toBeInTheDocument();
    expect(screen.getByText("second queued")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Send now" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0].closest("[data-turn]")).toHaveAttribute("data-turn", "q1");
  });

  test("a queue head already on its way (steering / interrupting) offers no Send now", async () => {
    for (const delivery of ["steering", "interrupting"]) {
      api.session.mockResolvedValue(
        snap({ active_turn: "t0", state: "running", turns: [queued({ turn_id: "q1", mine: true, delivery })] }),
      );
      api.events.mockResolvedValue({ link: "LINK-A", revision: 1, next_cursor: 1 });
      const r = render(
        <ApiView
          unlocked={{ share: share(), csrf: "c1" }}
          onShare={vi.fn()}
          onGone={vi.fn()}
          onExpired={vi.fn()}
          onRecheck={vi.fn(async () => {})}
        />,
      );
      expect(await screen.findByText("my queued message")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Send now" })).toBeNull();
      r.unmount();
    }
  });

  test("an agent's own turn that ended without a reply still says how it ended", async () => {
    api.session.mockResolvedValue(
      snap({
        turns: [
          { kind: "agent", turn_id: "g1", state: "interrupted", reply: null, reply_ts: null, reply_truncated: false, tools: [], tools_truncated: false },
        ],
      }),
    );
    api.events.mockResolvedValue({ link: "LINK-A", revision: 1, next_cursor: 1 });
    render(
      <ApiView
        unlocked={{ share: share({ mode: "view" }), csrf: "c1" }}
        onShare={vi.fn()}
        onGone={vi.fn()}
        onExpired={vi.fn()}
        onRecheck={vi.fn(async () => {})}
      />,
    );
    expect(await screen.findByText(/on its own · interrupted/)).toBeInTheDocument();
  });

  test("an answer for another link is never shown; the view re-reads its link", async () => {
    const onRecheck = vi.fn(async () => {});
    api.session.mockResolvedValueOnce(snap({ turns: [queued({ text: "link A turn", state: "completed" })] }));
    api.session.mockResolvedValue(
      snap({ link: "LINK-B", revision: 9, turns: [queued({ text: "LINK B PRIVATE", state: "completed" })] }),
    );
    api.events.mockResolvedValue({ link: "LINK-B", revision: 9, next_cursor: 9 });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(
        <ApiView
          unlocked={{ share: share({ mode: "view" }), csrf: "c1" }}
          onShare={vi.fn()}
          onGone={vi.fn()}
          onExpired={vi.fn()}
          onRecheck={onRecheck}
        />,
      );
      expect(await screen.findByText("link A turn")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(11_000);
      });
      await waitFor(() => expect(onRecheck).toHaveBeenCalled());
      expect(screen.getByText("reconnecting…")).toBeInTheDocument(); // stale until re-read
      expect(screen.queryByText("LINK B PRIVATE")).toBeNull();
      expect(screen.getByText("link A turn")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  test("when the cookie names another link, the viewer remounts for it — nothing carries over", async () => {
    window.history.replaceState(null, "", `/p/${TOKEN}`);
    api.unlock.mockResolvedValue({ share: share({ mode: "view", title: "Link A" }), csrf: "c1" });
    api.session.mockResolvedValueOnce(snap({ turns: [queued({ text: "link A turn", state: "completed" })] }));
    // The poll's events page answers for B; the view for B then reads its snapshot, held back:
    // in that gap the page must already show nothing of A under B's title.
    let releaseB!: (v: unknown) => void;
    api.session.mockReturnValueOnce(new Promise((r) => (releaseB = r)));
    api.session.mockResolvedValue(
      snap({ link: "LINK-B", revision: 2, turns: [queued({ text: "link B turn", state: "completed" })] }),
    );
    api.events.mockResolvedValue({ link: "LINK-B", revision: 2, next_cursor: 2 });
    api.share.mockResolvedValue({ share: share({ link: "LINK-B", mode: "view", title: "Link B" }), csrf: "c2" });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ShareViewer />);
      expect(await screen.findByText("link A turn")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(11_000);
      });
      expect(await screen.findByText("Link B")).toBeInTheDocument();
      expect(screen.queryByText("link A turn")).toBeNull(); // remounted: A's transcript is gone
      await act(async () =>
        releaseB(snap({ link: "LINK-B", revision: 2, turns: [queued({ text: "link B turn", state: "completed" })] })),
      );
      expect(await screen.findByText("link B turn")).toBeInTheDocument();
      // B's content only ever under B's title, and A's content is gone.
      expect(screen.getByText("Link B")).toBeInTheDocument();
      expect(screen.queryByText("Link A")).toBeNull();
      expect(screen.queryByText("link A turn")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
