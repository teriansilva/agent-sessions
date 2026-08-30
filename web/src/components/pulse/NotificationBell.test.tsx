import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import { NotificationBell } from "./NotificationBell";

/** #750 gave the panel two mount paths — an anchored dropdown inside the bell's wrapper on
 *  desktop, a portalled drawer on a phone. jsdom cannot judge either layout (that is what
 *  `e2e/mobile-pulse-layout.spec.ts` is for), but it CAN pin the thing a two-path refactor
 *  actually risks: the two paths drifting into two different panels. */

const NOTIFICATION = {
  id: "n1",
  title: "claude needs a decision",
  reason: "waiting on a menu choice",
  project: "agent-sessions",
  engine: "claude",
  session_id: "claude:abc",
  action_id: "a1",
  ts: Math.floor(Date.now() / 1000) - 60,
  read: false,
};

function mockWidth(isPhone: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((q: string) => ({
      matches: q.includes("640") ? isPhone : false,
      media: q,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

async function open(isPhone: boolean) {
  mockWidth(isPhone);
  vi.spyOn(api, "notifications").mockResolvedValue({
    notifications: [NOTIFICATION],
    unread: 1,
  });
  const { unmount } = render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /notifications/i }),
  );
  return {
    panel: screen.getByRole("dialog", { name: /notifications/i }),
    unmount,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("both mount paths render the same panel — same row, same deep link (#750)", async () => {
  const { panel: phone, unmount } = await open(true);
  await waitFor(() =>
    expect(phone).toHaveTextContent("claude needs a decision"),
  );
  expect(phone.querySelector('a[href="/s/claude/abc"]')).not.toBeNull();
  const phoneText = phone.textContent;
  unmount();

  const { panel: desk } = await open(false);
  await waitFor(() =>
    expect(desk).toHaveTextContent("claude needs a decision"),
  );
  expect(desk.querySelector('a[href="/s/claude/abc"]')).not.toBeNull();
  expect(desk.textContent).toBe(phoneText);
});

test("the drawer's scrim dismisses the panel (#750)", async () => {
  const { panel } = await open(true);
  await userEvent.click(
    screen.getByRole("button", { name: /dismiss notifications/i }),
  );
  expect(panel).not.toBeInTheDocument();
});

test("the dropdown has neither scrim nor close button (#750)", async () => {
  await open(false);
  // Both belong to the modal contract the drawer takes on. On desktop the panel is a plain
  // anchored dropdown: a scrim would dim the whole app, and the bell is never inert there so
  // it remains the close affordance.
  expect(
    screen.queryByRole("button", { name: /dismiss notifications/i }),
  ).toBeNull();
  expect(
    screen.queryByRole("button", { name: /close notifications/i }),
  ).toBeNull();
});

test("the drawer isolates the app root while open and lifts it on close (#750)", async () => {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  try {
    const { panel } = await open(true);
    // `aria-modal` is a claim about the background; this is that claim actually being true.
    expect(root.hasAttribute("inert")).toBe(true);
    await userEvent.click(
      screen.getByRole("button", { name: /close notifications/i }),
    );
    expect(panel).not.toBeInTheDocument();
    // An install left inert after close would be a dead app — far worse than the bug this
    // whole change fixes.
    expect(root.hasAttribute("inert")).toBe(false);
  } finally {
    root.remove();
  }
});

test("resolving an action refreshes the bell in the same tab (#800)", async () => {
  // The server retires the alert the moment the action settles, but the bell polls on a 60s
  // timer — so without this the badge keeps counting an escalation the operator just decided,
  // on the very screen where they decided it.
  mockWidth(false);
  const list = vi
    .spyOn(api, "notifications")
    .mockResolvedValue({ notifications: [NOTIFICATION], unread: 1 });
  render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText("1")).toBeTruthy());

  // …the action is resolved elsewhere in the tab, and the server now returns an empty bell.
  list.mockResolvedValue({ notifications: [], unread: 0 });
  window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));

  await waitFor(() => expect(screen.queryByText("1")).toBeNull());
});

// =============================================================================================
// The settled window and the `uncertain` count (#852, wired by #879).
// =============================================================================================

const SETTLED = {
  ...NOTIFICATION,
  id: "s1",
  title: "delivered: continue",
  read: true,
};

test("the settled window renders as history, with no controls", async () => {
  mockWidth(false);
  vi.spyOn(api, "notifications").mockResolvedValue({
    notifications: [NOTIFICATION],
    unread: 1,
    uncertain: 0,
    settled: [SETTLED],
  });
  render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("button", { name: /notifications/i }));
  const row = await screen.findByTestId("bell-settled-row");
  expect(row).toHaveTextContent("delivered: continue");
  // History offers nothing to decide — that is the whole contract of the window.
  expect(within(row).queryByRole("button")).toBeNull();
});

test("`uncertain` is shown as its own signal and NEVER added to the badge", async () => {
  mockWidth(false);
  vi.spyOn(api, "notifications").mockResolvedValue({
    notifications: [NOTIFICATION],
    unread: 1,
    uncertain: 3,
    settled: [],
  });
  render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>,
  );
  const bell = await screen.findByRole("button", { name: /notifications/i });
  // 1, not 4. A row whose state could not be read offers no control, so counting it as
  // actionable hands the operator a number they cannot clear by acting (#852 rule 5).
  expect(bell).toHaveAccessibleName(/1 unread/i);
  await userEvent.click(bell);
  expect(await screen.findByTestId("bell-uncertain")).toHaveTextContent(
    /3 decisions could not be read/i,
  );
});

test("Clear sends exactly the settled ids that were DISPLAYED (#862)", async () => {
  // The race this exists for: the operator sees one settled row, another decision settles
  // before the click, and a server-side "clear the current window" would hide the second one
  // without it ever being seen — permanently, since hidden is what keeps a row out of every
  // later projection.
  mockWidth(false);
  const clear = vi
    .spyOn(api, "clearSettledNotifications")
    .mockResolvedValue({ cleared: 1 });
  vi.spyOn(api, "notifications").mockResolvedValue({
    notifications: [],
    unread: 0,
    uncertain: 0,
    settled: [SETTLED],
  });
  render(
    <MemoryRouter>
      <NotificationBell />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("button", { name: /notifications/i }));
  await screen.findByTestId("bell-settled-row");

  // …a second decision settles between the render and the click. The client must not send it.
  vi.spyOn(api, "notifications").mockResolvedValue({
    notifications: [],
    unread: 0,
    uncertain: 0,
    settled: [SETTLED, { ...SETTLED, id: "s2", title: "rejected: continue" }],
  });

  await userEvent.click(screen.getByTestId("bell-clear-settled"));
  await waitFor(() => expect(clear).toHaveBeenCalled());
  expect(clear).toHaveBeenCalledWith(["s1"]);
});
