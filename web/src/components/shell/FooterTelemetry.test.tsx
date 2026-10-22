/** The footer's live telemetry (#1531): one shared poll, numbers formatted as the design says, an
 *  estimate marked "≈", and an honest "unavailable" — never classification text. */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import { formatTokens, gb, resetTelemetryForTests, TELEMETRY_POLL_MS } from "../../lib/useTelemetry";
import type { Telemetry } from "../../types/api";
import { FooterTelemetry } from "./FooterTelemetry";

const SAMPLE: Telemetry = {
  cpu_pct: 23.4,
  mem: { used: 9.8 * 2 ** 30, total: 16 * 2 ** 30, pct: 61.2 },
  hq: { ctx_tokens: 18234, ctx_estimated: false, today_tokens: 412000, today_estimated: true, at: 1 },
  as_of: 1,
};

beforeEach(() => {
  vi.useFakeTimers();
  resetTelemetryForTests();
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  resetTelemetryForTests();
});

test("formats tokens and gigabytes the way the footer prints them", () => {
  expect(formatTokens(999)).toBe("999");
  expect(formatTokens(18234)).toBe("18.2K");
  expect(formatTokens(412000)).toBe("412K");
  expect(formatTokens(1_250_000)).toBe("1.25M");
  expect(gb(9.8 * 2 ** 30)).toBe("9.8");
});

test("renders CPU, RAM and HQ tokens; an estimate carries ≈ (#1531)", async () => {
  vi.spyOn(api, "telemetry").mockResolvedValue(SAMPLE);
  render(<FooterTelemetry />);
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByTestId("tm-cpu")).toHaveTextContent("23%");
  expect(screen.getByTestId("tm-ram")).toHaveTextContent("61%");
  expect(screen.getByTestId("tm-ctx")).toHaveTextContent("18.2K");
  expect(screen.getByTestId("tm-today")).toHaveTextContent("≈412K");
  expect(screen.getByTestId("footer-telemetry")).not.toHaveTextContent(/classified|unclassified/i);
});

test("two readers share ONE poll, every 5 s, and a hidden tab asks nothing (#1531)", async () => {
  const spy = vi.spyOn(api, "telemetry").mockResolvedValue(SAMPLE);
  render(
    <>
      <FooterTelemetry />
      <FooterTelemetry />
    </>,
  );
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(TELEMETRY_POLL_MS);
  });
  expect(spy).toHaveBeenCalledTimes(2);
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(TELEMETRY_POLL_MS * 3);
  });
  expect(spy).toHaveBeenCalledTimes(2);
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();
  });
  expect(spy).toHaveBeenCalledTimes(3);
});

test("a failed first read says TELEMETRY // UNAVAILABLE (#1531)", async () => {
  vi.spyOn(api, "telemetry").mockRejectedValue(new Error("401"));
  render(<FooterTelemetry />);
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByTestId("footer-telemetry")).toHaveAttribute("data-state", "unavailable");
  expect(screen.getByTestId("footer-telemetry")).toHaveTextContent(/unavailable/i);
});
