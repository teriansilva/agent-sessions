import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";

import { MotionProvider } from "../../theme/MotionProvider";
import { useMotion } from "../../theme/motionStore";
import { CONTACTS } from "./contacts";
import { HqPortrait } from "./HqPortrait";

const lupu = CONTACTS.find((c) => c.id === "romania-woman")!;

function motion(reduce: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

let play: ReturnType<typeof vi.fn>;
let pause: ReturnType<typeof vi.fn>;

beforeEach(() => {
  motion(false);
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn();
  Object.defineProperty(HTMLMediaElement.prototype, "play", { configurable: true, value: play });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: pause });
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

afterEach(() => {
  // @ts-expect-error -- jsdom has no matchMedia of its own
  delete window.matchMedia;
  localStorage.removeItem("tr-motion");
  delete document.documentElement.dataset.motion;
  delete document.documentElement.dataset.motionPaused;
});

test("a muted, looping, decorative video with the WebM first and the poster", () => {
  render(<HqPortrait contact={lupu} playing />);
  const v = screen.getByTestId("hq-portrait") as HTMLVideoElement;
  expect(v.tagName).toBe("VIDEO");
  expect(v).toHaveAttribute("aria-hidden", "true");
  expect(v).toHaveAttribute("poster", "/hq/romania-woman.jpg");
  expect(v.loop).toBe(true);
  expect(v.muted).toBe(true);
  expect([...v.querySelectorAll("source")].map((s) => s.getAttribute("type"))).toEqual([
    "video/webm",
    "video/mp4",
  ]);
  expect(v).toHaveAttribute("data-state", "playing");
  expect(play).toHaveBeenCalled();
});

/** Renders under the real MotionProvider, so the portrait follows the ONE motion mode (#1531). */
function renderIn(choice: string | null, ui: ReactElement) {
  if (choice) localStorage.setItem("tr-motion", choice);
  else localStorage.removeItem("tr-motion");
  return render(<MotionProvider>{ui}</MotionProvider>);
}

for (const choice of ["reduced", "off"]) {
  test(`in the ${choice} motion mode only the poster renders: no <video> at all (#1531)`, () => {
    const { container } = renderIn(choice, <HqPortrait contact={lupu} playing />);
    expect(container.querySelector("video")).toBeNull();
    const img = screen.getByTestId("hq-portrait");
    expect(img).toHaveAttribute("src", "/hq/romania-woman.jpg");
    expect(img).toHaveAttribute("data-state", "still");
    expect(play).not.toHaveBeenCalled();
  });
}

test("Match device on a reduce-motion device is Reduced: the poster (#1531)", () => {
  motion(true);
  const { container } = renderIn(null, <HqPortrait contact={lupu} playing />);
  expect(container.querySelector("video")).toBeNull();
});

test("an explicit Full plays the loop even where the device asks to reduce motion (#1531)", () => {
  motion(true);
  renderIn("full", <HqPortrait contact={lupu} playing />);
  expect(screen.getByTestId("hq-portrait").tagName).toBe("VIDEO");
  expect(play).toHaveBeenCalled();
});

test("a live switch Full → Off → Full swaps the loop for the poster and back (#1531)", async () => {
  renderIn("full", <MotionSwitcher />);
  expect(screen.getByTestId("hq-portrait").tagName).toBe("VIDEO");
  await userEvent.click(screen.getByRole("button", { name: "off" }));
  expect(screen.getByTestId("hq-portrait").tagName).toBe("IMG");
  await userEvent.click(screen.getByRole("button", { name: "full" }));
  expect(screen.getByTestId("hq-portrait").tagName).toBe("VIDEO");
});

test("an idle or hidden pause stops the loop on its frame (#1531)", async () => {
  renderIn("full", <HqPortrait contact={lupu} playing />);
  expect(play).toHaveBeenCalledTimes(1);
  await act(async () => {
    document.documentElement.dataset.motionPaused = "idle";
    await Promise.resolve();
  });
  expect(pause).toHaveBeenCalled();
  expect(screen.getByTestId("hq-portrait")).toHaveAttribute("data-state", "paused");
  await act(async () => {
    delete document.documentElement.dataset.motionPaused;
    await Promise.resolve();
  });
  expect(play).toHaveBeenCalledTimes(2);
});

function MotionSwitcher() {
  const { setMotion } = useMotion();
  return (
    <>
      <button type="button" onClick={() => setMotion("off")}>
        off
      </button>
      <button type="button" onClick={() => setMotion("full")}>
        full
      </button>
      <HqPortrait contact={lupu} playing />
    </>
  );
}

test("it pauses when the panel closes, and again when the tab is hidden", () => {
  const { rerender } = render(<HqPortrait contact={lupu} playing />);
  expect(play).toHaveBeenCalledTimes(1);
  rerender(<HqPortrait contact={lupu} playing={false} />);
  expect(pause).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("hq-portrait")).toHaveAttribute("data-state", "paused");

  rerender(<HqPortrait contact={lupu} playing />);
  expect(play).toHaveBeenCalledTimes(2);
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(pause).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("hq-portrait")).toHaveAttribute("data-state", "paused");
});

test("a loop that fails to load (its last source errors) falls back to the poster", () => {
  const { container } = render(<HqPortrait contact={lupu} playing />);
  const sources = container.querySelectorAll("source");
  act(() => {
    fireEvent.error(sources[sources.length - 1]);
  });
  expect(container.querySelector("video")).toBeNull();
  const img = screen.getByTestId("hq-portrait");
  expect(img).toHaveAttribute("src", "/hq/romania-woman.jpg");
  expect(img).toHaveAttribute("data-state", "failed");
});
