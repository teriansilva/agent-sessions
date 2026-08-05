import { render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ButtonGlitch } from "./ButtonGlitch";
import { DataFlowCanvas } from "./DataFlowCanvas";
import { SysClock } from "./SysClock";

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi
      .fn()
      .mockReturnValue({
        matches: reduce,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("SysClock renders a SYS // UTC readout", () => {
  const { getByText, container } = render(<SysClock />);
  expect(getByText(/SYS \/\//)).toBeInTheDocument();
  // a HH:MM:SSZ time sits in the tabular-num slot
  expect(container.querySelector(".num")?.textContent).toMatch(
    /^\d{2}:\d{2}:\d{2}Z$/,
  );
});

test("DataFlowCanvas renders an aria-hidden #bg canvas and survives a null 2d context", () => {
  // jsdom's getContext returns null → the effect must bail without throwing.
  const { container } = render(<DataFlowCanvas />);
  const cv = container.querySelector("canvas#bg");
  expect(cv).toBeInTheDocument();
  expect(cv).toHaveAttribute("aria-hidden", "true");
});

test("ButtonGlitch renders nothing", () => {
  const { container } = render(<ButtonGlitch />);
  expect(container).toBeEmptyDOMElement();
});

test("ButtonGlitch briefly glitches a visible .shine button, then clears it", () => {
  mockReducedMotion(false);
  // random=0 → fixed 7000ms schedule, so we can land between the add and the +300ms clear.
  const rand = vi.spyOn(Math, "random").mockReturnValue(0);
  vi.useFakeTimers();
  const btn = document.createElement("button");
  btn.className = "shine";
  // jsdom doesn't compute layout → offsetParent is null; force it visible for the filter.
  Object.defineProperty(btn, "offsetParent", {
    get: () => document.body,
    configurable: true,
  });
  document.body.appendChild(btn);
  try {
    render(<ButtonGlitch />);
    vi.advanceTimersByTime(7001); // just past the first schedule (add), before the +300 clear
    expect(btn.classList.contains("glitching")).toBe(true);
    vi.advanceTimersByTime(300); // the clear timeout fires
    expect(btn.classList.contains("glitching")).toBe(false);
  } finally {
    btn.remove();
    rand.mockRestore();
  }
});

test("ButtonGlitch is a no-op under prefers-reduced-motion", () => {
  mockReducedMotion(true);
  vi.useFakeTimers();
  const btn = document.createElement("button");
  btn.className = "shine";
  Object.defineProperty(btn, "offsetParent", {
    get: () => document.body,
    configurable: true,
  });
  document.body.appendChild(btn);
  try {
    render(<ButtonGlitch />);
    vi.advanceTimersByTime(60000);
    expect(btn.classList.contains("glitching")).toBe(false);
  } finally {
    btn.remove();
  }
});
