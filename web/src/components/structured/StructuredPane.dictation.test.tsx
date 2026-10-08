import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { setRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot } from "../../types/api";
import { StructuredPane } from "./StructuredPane";

// Push-to-talk in the API composer (#1332 Phase 3c): the terminal composer's recognizer lifecycle
// (`useDictation`), writing into this draft. The lifecycle's own edge cases are pinned in
// Compose.test.tsx; these pin the API pane's wiring.

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      structuredDecide: vi.fn(),
      structuredInterrupt: vi.fn(),
      structuredStop: vi.fn(),
      upload: vi.fn(),
      uploadBlob: vi.fn(() => new Promise(() => {})),
      templates: vi.fn(() => new Promise(() => {})),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;

function snap(over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

let last: FakeRecognition | null = null;

class FakeRecognition {
  continuous = false;
  interimResults = false;
  lang = "";
  onresult: ((ev: SpeechRecognitionEvent) => void) | null = null;
  onerror: ((ev: SpeechRecognitionErrorEvent) => void) | null = null;
  onend: ((ev: Event) => void) | null = null;
  start = vi.fn();
  /** A real engine stops capturing, THEN delivers its tail, THEN ends. */
  stop = vi.fn();
  abort = vi.fn();
  constructor() {
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- test double exposes its instance
    last = this;
  }
  say(transcript: string, isFinal: boolean) {
    this.onresult?.({
      resultIndex: 0,
      results: [{ 0: { transcript, confidence: 1 }, isFinal, length: 1 }],
    } as unknown as SpeechRecognitionEvent);
  }
  end() {
    this.onend?.(new Event("end"));
  }
}

const HOLD = { pointerId: 1, pointerType: "mouse", button: 0 };
const mic = () => screen.getByRole("button", { name: /voice input/i });

function renderPane() {
  return render(
    <MemoryRouter>
      <StructuredPane engine="codex-api" id={ID} />
    </MemoryRouter>,
  );
}

async function hold() {
  fireEvent.pointerDown(mic(), HOLD);
  await waitFor(() => expect(last).not.toBeNull());
}

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap());
  vi.mocked(api.structuredEvents).mockResolvedValue({ session_key: KEY, revision: 4, next_cursor: 4, events: [] });
  vi.mocked(api.structuredSubmit).mockReset().mockResolvedValue({});
  window.SpeechRecognition = FakeRecognition as unknown as typeof window.SpeechRecognition;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: vi.fn(() =>
        Promise.resolve({ getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream),
      ),
    },
  });
});

afterEach(() => {
  last = null;
  delete window.SpeechRecognition;
  delete window.webkitSpeechRecognition;
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
});

test("no speech engine, no push-to-talk", async () => {
  delete window.SpeechRecognition;
  renderPane();
  await screen.findByRole("textbox");
  expect(screen.queryByRole("button", { name: /voice input/i })).toBeNull();
});

test("hold to talk writes into the draft; the tail after release lands; then it sends", async () => {
  renderPane();
  const box = await screen.findByRole("textbox");
  await hold();
  act(() => last!.say("run the logout", false));
  expect(box).toHaveValue("run the logout");
  fireEvent.pointerUp(mic(), HOLD);
  expect(last!.stop).toHaveBeenCalled();
  expect(mic()).toHaveAttribute("aria-label", "Finishing voice input");
  act(() => last!.say("run the logout spec", true)); // the engine's tail, after release
  act(() => last!.end());
  expect(box).toHaveValue("run the logout spec");
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalled());
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][2]).toBe("run the logout spec");
});

test("Send during a hold waits for the tail — never a half sentence", async () => {
  renderPane();
  const box = await screen.findByRole("textbox");
  await hold();
  act(() => last!.say("deploy to", false));
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(last!.stop).toHaveBeenCalled(); // the send ended capture like a release
  expect(api.structuredSubmit).not.toHaveBeenCalled(); // held until the draft settles
  act(() => last!.say("deploy to staging", true));
  act(() => last!.end());
  await waitFor(() => expect(api.structuredSubmit).toHaveBeenCalled());
  expect(vi.mocked(api.structuredSubmit).mock.calls[0][2]).toBe("deploy to staging");
  await waitFor(() => expect(box).toHaveValue(""));
});

test("while dictating, Templates cannot replace the draft the recognizer is writing", async () => {
  renderPane();
  await screen.findByRole("textbox");
  expect(screen.getByRole("button", { name: "Use a template" })).toBeEnabled();
  await hold();
  expect(screen.getByRole("button", { name: "Use a template" })).toBeDisabled();
  fireEvent.pointerUp(mic(), HOLD);
  act(() => last!.end());
  await waitFor(() => expect(screen.getByRole("button", { name: "Use a template" })).toBeEnabled());
});

test("Space outside the chip never starts dictation in the API pane (the terminal composer owns it)", async () => {
  renderPane();
  await screen.findByRole("textbox");
  fireEvent.keyDown(document.body, { key: " " });
  await new Promise((r) => setTimeout(r, 20));
  expect(last).toBeNull();
});
