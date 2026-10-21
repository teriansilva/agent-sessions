import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

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

test("under reduced motion only the poster renders: no <video> at all", () => {
  motion(true);
  const { container } = render(<HqPortrait contact={lupu} playing />);
  expect(container.querySelector("video")).toBeNull();
  const img = screen.getByTestId("hq-portrait");
  expect(img).toHaveAttribute("src", "/hq/romania-woman.jpg");
  expect(img).toHaveAttribute("data-state", "still");
  expect(play).not.toHaveBeenCalled();
});

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
