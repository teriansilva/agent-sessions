import { describe, expect, test } from "vitest";
import {
  SESSION_LIMIT_FALLBACK_MS,
  sessionExpiredMessage,
  sessionExpiryMs,
} from "./sessionWindow";

const NOW = 1_750_000_000_000; // arbitrary fixed ms epoch

describe("sessionExpiryMs", () => {
  test("no deadline → 4-hour fallback window", () => {
    expect(sessionExpiryMs(NOW)).toBe(NOW + SESSION_LIMIT_FALLBACK_MS);
    expect(sessionExpiryMs(NOW, undefined)).toBe(
      NOW + SESSION_LIMIT_FALLBACK_MS,
    );
    expect(sessionExpiryMs(NOW, 0)).toBe(NOW + SESSION_LIMIT_FALLBACK_MS);
  });

  test("a 4-hour relay deadline is NOT clamped back to one hour (#662 regression)", () => {
    const deadlineSec = NOW / 1000 + 14400;
    const expiry = sessionExpiryMs(NOW, deadlineSec);
    expect(expiry).toBe(deadlineSec * 1000);
    // The old code clamped to min(now + 1h, deadline); prove we outlive that.
    expect(expiry).toBeGreaterThan(NOW + 60 * 60 * 1000);
  });

  test("a shorter relay deadline wins over the fallback", () => {
    const deadlineSec = NOW / 1000 + 600;
    expect(sessionExpiryMs(NOW, deadlineSec)).toBe(deadlineSec * 1000);
  });

  test("a relay deadline LONGER than four hours is honored, not capped (relay owns the timer)", () => {
    const deadlineSec = NOW / 1000 + 6 * 3600;
    const expiry = sessionExpiryMs(NOW, deadlineSec);
    expect(expiry).toBe(deadlineSec * 1000);
    expect(expiry).toBeGreaterThan(NOW + SESSION_LIMIT_FALLBACK_MS);
  });

  test("an already-expired deadline yields a past expiry (caller clears the session)", () => {
    const deadlineSec = NOW / 1000 - 10;
    expect(sessionExpiryMs(NOW, deadlineSec)).toBeLessThan(NOW);
  });

  test("expiry copy names the 4-hour limit", () => {
    expect(sessionExpiredMessage()).toBe("session expired (4-hour limit)");
    expect(sessionExpiredMessage()).toContain("4-hour");
  });
});
