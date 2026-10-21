import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import {
  getRoster,
  loadRuntimeHints,
  resetRoster,
  RUNTIME_HINTS_KEY,
  setRoster,
} from "../../app/engineRoster";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import { RuntimeGate } from "./RuntimeGate";
import { OPENING_GIVE_UP_MS } from "./SessionOpening";

// The structured pane as a marker: which identity, and was its runtime confirmed?
vi.mock("../structured/StructuredPane", () => ({
  StructuredPane: ({
    engine,
    id,
    runtimeConfirmed,
  }: {
    engine: string;
    id: string;
    runtimeConfirmed?: boolean;
  }) => (
    <div data-testid="structured" data-confirmed={String(runtimeConfirmed)}>
      {`${engine}:${id}`}
    </div>
  ),
}));

// The chat pane as a marker too.
vi.mock("../chat/ChatPane", () => ({
  ChatPane: ({
    engine,
    id,
    runtimeConfirmed,
  }: {
    engine: string;
    id: string;
    runtimeConfirmed?: boolean;
  }) => (
    <div data-testid="chat" data-confirmed={String(runtimeConfirmed)}>
      {`${engine}:${id}`}
    </div>
  ),
}));

// #853 §7: the session surface is chosen by the engine's runtime, never assumed. #1502: before
// the roster has loaded, by the runtime the last loaded roster recorded in this browser.

const ENGINES = fixture.engines as unknown as EngineInfo[];
// Engine ids come from the generated roster fixture, by runtime — never a literal list here.
const PTY = ENGINES.find((e) => e.runtime === "pty")!.id;
const API = ENGINES.find((e) => e.runtime === "api")!.id;
const CHAT = ENGINES.find((e) => e.runtime === "chat")!.id;
const ID = "24c5e209-0000-4000-8000-000000000001";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
});

/** A browser that has never loaded a roster: nothing in storage, nothing in memory. */
function freshBrowser() {
  localStorage.clear();
  resetRoster();
}

/** A returning browser on a cold page load: storage holds the last roster's runtimes, the roster
 *  itself has not loaded yet. */
function returningBrowser(runtimes: Record<string, string>) {
  resetRoster();
  localStorage.setItem(RUNTIME_HINTS_KEY, JSON.stringify({ v: 1, runtimes }));
  loadRuntimeHints();
}

function gate(engine: string) {
  return (
    <MemoryRouter>
      <RuntimeGate engine={engine} id={ID}>
        TERMINAL
      </RuntimeGate>
    </MemoryRouter>
  );
}

test("a pty engine gets its terminal", () => {
  render(gate(PTY));
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("an engine of another runtime gets the explicit panel, never a terminal", () => {
  setRoster(getRoster().engines.map((e) => (e.id === PTY ? { ...e, runtime: "other" } : e)));
  render(gate(PTY));
  expect(screen.queryByText("TERMINAL")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent(/needs a newer BattleLab/);
  expect(screen.getByRole("status")).toHaveTextContent(/Nothing was started/);
});

test("a loaded roster that does not list the engine still renders the terminal (the server decides)", () => {
  render(gate("zeta"));
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("no hint and no roster yet: 'Opening…', never a terminal — then the roster's pane (#1502)", () => {
  freshBrowser();
  render(gate(API));
  expect(screen.queryByText("TERMINAL")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Opening this session…");
  act(() => setRoster(ENGINES));
  expect(screen.getByTestId("structured")).toHaveTextContent(`${API}:${ID}`);
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "true");
});

test("no hint, roster loads as pty: the terminal mounts after one roster fetch (#1502)", () => {
  freshBrowser();
  render(gate(PTY));
  expect(screen.queryByText("TERMINAL")).toBeNull();
  act(() => setRoster(ENGINES));
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("a cached api hint draws the structured pane at once, unconfirmed until the roster lands (#1502)", () => {
  returningBrowser({ [API]: "api", [PTY]: "pty" });
  render(gate(API));
  expect(screen.queryByText("TERMINAL")).toBeNull();
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "false");
  act(() => setRoster(ENGINES));
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "true");
});

test("a cached pty hint mounts the terminal without waiting for the roster (#1502)", () => {
  returningBrowser({ [API]: "api", [PTY]: "pty" });
  render(gate(PTY));
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("a stale api hint yields to the roster: the engine is a terminal engine now (#1502)", () => {
  returningBrowser({ [PTY]: "api" });
  render(gate(PTY));
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "false");
  act(() => setRoster(ENGINES));
  expect(screen.queryByTestId("structured")).toBeNull();
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("a roster that never arrives turns 'Opening…' into Retry after the bound; Retry reloads (#1502)", async () => {
  vi.useFakeTimers();
  freshBrowser();
  const engines = vi.spyOn(api, "engines").mockRejectedValue(new Error("offline"));
  render(gate(API));
  expect(screen.getByRole("status")).toHaveTextContent("Opening this session…");
  act(() => vi.advanceTimersByTime(OPENING_GIVE_UP_MS - 1));
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load the agent list.");
  expect(screen.getByRole("alert")).toHaveTextContent("Nothing was started");
  // Retry asks NOW; a failure comes straight back as the Retry state.
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(engines).toHaveBeenCalledTimes(1);
  await act(async () => {});
  expect(screen.getByRole("alert")).toBeInTheDocument();
  // A retry that works installs the roster, and the gate draws the right pane.
  engines.mockResolvedValue({ engines: ENGINES, problems: [] } as never);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await act(async () => {});
  expect(screen.getByTestId("structured")).toHaveTextContent(`${API}:${ID}`);
});

// Hermes on #1522: the chat branch is a cached hint too, in both directions.
test("a stale chat hint for an engine that is api now: chat pane unconfirmed, then the structured pane (#1502)", () => {
  returningBrowser({ [API]: "chat" });
  render(gate(API));
  expect(screen.getByTestId("chat")).toHaveAttribute("data-confirmed", "false");
  act(() => setRoster(ENGINES));
  expect(screen.queryByTestId("chat")).toBeNull();
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "true");
});

test("a stale api hint for an engine that is chat now: structured unconfirmed, then the chat pane (#1502)", () => {
  returningBrowser({ [CHAT]: "api" });
  render(gate(CHAT));
  expect(screen.getByTestId("structured")).toHaveAttribute("data-confirmed", "false");
  act(() => setRoster(ENGINES));
  expect(screen.queryByTestId("structured")).toBeNull();
  expect(screen.getByTestId("chat")).toHaveAttribute("data-confirmed", "true");
});

test("a cached runtime this build cannot draw waits for the roster instead of a verdict (#1502)", () => {
  returningBrowser({ [PTY]: "future" });
  render(gate(PTY));
  expect(screen.getByRole("status")).toHaveTextContent("Opening this session…");
  act(() => setRoster(ENGINES));
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});
