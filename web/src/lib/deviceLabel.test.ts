import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { getDeviceLabel, setDeviceLabel } from "./deviceLabel";

function setUA(ua: string) {
  Object.defineProperty(navigator, "userAgent", {
    value: ua,
    configurable: true,
  });
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
});

test("falls back to an OS · Browser default from the UA (#293)", () => {
  setUA(
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  );
  expect(getDeviceLabel()).toBe("iPhone · Safari");
  setUA(
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  );
  expect(getDeviceLabel()).toBe("Mac · Chrome");
});

test("a saved label overrides the UA default; clearing restores it (#293)", () => {
  setUA("Mozilla/5.0 (Macintosh) Chrome/120.0 Safari/537.36");
  setDeviceLabel("Marcus's MacBook");
  expect(getDeviceLabel()).toBe("Marcus's MacBook");
  setDeviceLabel("");
  expect(getDeviceLabel()).toBe("Mac · Chrome"); // back to the default
});

test("the label is length-capped (#293)", () => {
  setDeviceLabel("x".repeat(200));
  expect(getDeviceLabel().length).toBe(80);
});
