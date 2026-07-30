import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
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
