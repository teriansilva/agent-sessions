/** The Ask sidebar (#1294): what the panel adds around `AskConsole`, whose own behaviour is
 *  `AskConsole.test.tsx`'s.
 *
 *  - It is not mounted until first opened, then KEPT: a conversation outlives closing the panel and
 *    navigating, and is ended only by New conversation (or a reload).
 *  - NEEDS YOU is read only while the panel is open.
 *  - ≤800px it is a modal drawer (`role="dialog"`, `aria-modal`, a scrim); above that it is a
 *    non-modal complementary panel that Escape closes, handing focus back to the icon.
 *  - With no AI endpoint, it says so — the notice the dashboard used to carry.
 */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCallback, useMemo, useRef, useState } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AppConfig } from "../../types/api";

const config = vi.hoisted(() => ({ value: undefined as AppConfig | undefined }));
vi.mock("../../app/config", () => ({
  useConfig: () => config.value,
  useConfigRefresh: () => () => {},
}));
vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { pulseAskStream: vi.fn(), needsYou: vi.fn() },
  };
});

import { api } from "../../lib/api";
import { AskPanelContext, type AskPanel } from "./askPanel";
import { AskSidebar, AskToggle } from "./AskSidebar";

function Shell() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const openAsk = useCallback(() => setOpen(true), []);
  const close = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((o) => !o), []);
  const value = useMemo<AskPanel>(
    () => ({ open, openAsk, close, toggle, triggerRef }),
    [open, openAsk, close, toggle],
  );
  return (
    <AskPanelContext.Provider value={value}>
      <MemoryRouter initialEntries={["/one"]}>
        <AskToggle />
        <Link to="/two">go two</Link>
        <Routes>
          <Route path="/one" element={<div>page one</div>} />
          <Route path="/two" element={<div>page two</div>} />
        </Routes>
        <AskSidebar />
      </MemoryRouter>
    </AskPanelContext.Provider>
  );
}

function setDrawer(matches: boolean) {
  window.matchMedia = vi.fn().mockReturnValue({
    matches,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
}

const realMatchMedia = window.matchMedia;
beforeEach(() => {
  config.value = { pulse: { configured: true } } as AppConfig;
  vi.mocked(api.needsYou).mockResolvedValue({
    rows: [],
    total: 0,
    total_unfiltered: 0,
    needs_you_ids: [],
  } as never);
  vi.mocked(api.pulseAskStream).mockImplementation(async (q, _h, onEvent) => {
    onEvent({
      type: "answer",
      final: true,
      answer: `answer to ${q}`,
      matches: [],
      stage: "catalog",
      configured: true,
    });
  });
  setDrawer(false);
});
afterEach(() => {
  window.matchMedia = realMatchMedia;
  vi.clearAllMocks();
});

test("not mounted until first opened; NEEDS YOU is read only while open", async () => {
  render(<Shell />);
  expect(screen.queryByTestId("ask-sidebar")).toBeNull();
  expect(api.needsYou).not.toHaveBeenCalled();
  await userEvent.click(screen.getByTestId("ask-toggle"));
  expect(screen.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
  expect(api.needsYou).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Close Ask" }));
  const n = vi.mocked(api.needsYou).mock.calls.length;
  // Closed: still mounted, hidden from assistive tech and the keyboard, and not polling.
  const panel = screen.getByTestId("ask-sidebar");
  expect(panel).toHaveAttribute("data-open", "false");
  expect(panel).toHaveAttribute("aria-hidden", "true");
  expect(panel.hasAttribute("inert")).toBe(true);
  vi.useFakeTimers();
  try {
    act(() => {
      vi.advanceTimersByTime(120_000);
    });
  } finally {
    vi.useRealTimers();
  }
  expect(vi.mocked(api.needsYou).mock.calls.length).toBe(n);
});

test("a conversation survives closing the panel AND navigating; New conversation ends it", async () => {
  render(<Shell />);
  await userEvent.click(screen.getByTestId("ask-toggle"));
  await userEvent.type(screen.getByTestId("composer-input"), "where?{Enter}");
  expect(await screen.findByText("answer to where?")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Close Ask" }));
  await userEvent.click(screen.getByRole("link", { name: "go two" }));
  expect(screen.getByText("page two")).toBeInTheDocument();
  await userEvent.click(screen.getByTestId("ask-toggle"));
  expect(screen.getByText("answer to where?")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
  expect(screen.queryByText("answer to where?")).toBeNull();
});

test("desktop: a non-modal panel; the question box takes focus; Escape closes it and returns focus to the icon", async () => {
  render(<Shell />);
  const toggle = screen.getByTestId("ask-toggle");
  await userEvent.click(toggle);
  const panel = screen.getByTestId("ask-sidebar");
  expect(panel).toHaveAttribute("role", "complementary");
  expect(panel).not.toHaveAttribute("aria-modal");
  expect(screen.queryByRole("button", { name: "Dismiss Ask" })).toBeNull();
  expect(screen.getByTestId("composer-input")).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(panel).toHaveAttribute("data-open", "false");
  expect(toggle).toHaveFocus();
});

test("phone: a modal drawer with a scrim; focus goes to Close; the scrim dismisses it", async () => {
  setDrawer(true);
  render(<Shell />);
  await userEvent.click(screen.getByTestId("ask-toggle"));
  const panel = screen.getByRole("dialog", { name: "Ask" });
  expect(panel).toHaveAttribute("aria-modal", "true");
  expect(within(panel).getByRole("button", { name: "Close Ask" })).toHaveFocus();
  await userEvent.click(screen.getByRole("button", { name: "Dismiss Ask" }));
  expect(screen.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "false");
});

test("no AI endpoint: the sidebar says so and links to the page that fixes it", async () => {
  config.value = { pulse: { configured: false } } as AppConfig;
  render(<Shell />);
  await userEvent.click(screen.getByTestId("ask-toggle"));
  expect(screen.getByTestId("ask-needs-endpoint")).toHaveTextContent(
    /needs an ai endpoint/i,
  );
  expect(
    screen.getByRole("link", { name: /endpoint & model/i }),
  ).toHaveAttribute("href", "/settings/ai-endpoint");
  expect(screen.getByTestId("composer-input")).toBeDisabled();
});
