import { render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ButtonGlitch } from "./ButtonGlitch";
import { DataFlowCanvas } from "./DataFlowCanvas";
import { SysClock } from "./SysClock";

function mockReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
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

test.each([
  "<button>Action</button>",
  '<a class="shine" href="/">New session</a>',
  '<nav class="section-nav"><a href="/mission">Missions</a></nav>',
  '<a class="gear" href="/settings">Settings</a>',
  '<div role="button" tabindex="0">Action</div>',
  '<input type="submit" value="Save">',
])("ButtonGlitch animates enabled button controls: %s", (markup) => {
  mockReducedMotion(false);
  // random=0 → fixed 7000ms schedule, so we can land between the add and the +300ms clear.
  const rand = vi.spyOn(Math, "random").mockReturnValue(0);
  vi.useFakeTimers();
  const host = document.createElement("div");
  host.innerHTML = markup;
  const btn = host.querySelector<HTMLElement>(
    "button, a, [role=button], input",
  )!;
  // jsdom doesn't compute layout → offsetParent is null; force it visible for the filter.
  Object.defineProperty(btn, "offsetParent", {
    get: () => document.body,
    configurable: true,
  });
  document.body.appendChild(host);
  try {
    render(<ButtonGlitch />);
    vi.advanceTimersByTime(7001); // just past the first schedule (add), before the +300 clear
    expect(btn.classList.contains("glitching")).toBe(true);
    vi.advanceTimersByTime(300); // the clear timeout fires
    expect(btn.classList.contains("glitching")).toBe(false);
  } finally {
    host.remove();
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

test("ambient glitch skips unavailable controls and cleans up the active control", () => {
  mockReducedMotion(false);
  vi.useFakeTimers();
  const rand = vi.spyOn(Math, "random").mockReturnValue(0);
  const host = document.createElement("div");
  host.innerHTML = `
    <button disabled>Disabled</button>
    <fieldset disabled><button>Disabled by fieldset</button></fieldset>
    <button aria-disabled="true">ARIA disabled</button>
    <div aria-disabled="true"><button>Unavailable group</button></div>
    <div inert><button>Inert</button></div>
    <div hidden><button>Hidden</button></div>
    <div aria-hidden="true"><button>ARIA hidden</button></div>
    <button style="visibility:hidden">Invisible</button>
    <button style="opacity:0">Transparent</button>
    <button id="enabled">Enabled</button>`;
  document.body.appendChild(host);
  for (const button of host.querySelectorAll("button")) {
    Object.defineProperty(button, "offsetParent", { get: () => document.body });
  }
  try {
    const { unmount } = render(<ButtonGlitch />);
    vi.advanceTimersByTime(7001);
    expect([...host.querySelectorAll(".glitching")]).toEqual([
      host.querySelector("#enabled"),
    ]);
    unmount();
    expect(host.querySelectorAll(".glitching")).toHaveLength(0);
    vi.advanceTimersByTime(60000);
    expect(host.querySelectorAll(".glitching")).toHaveLength(0);
  } finally {
    host.remove();
    rand.mockRestore();
  }
});
