import { describe, expect, test } from "vitest";
import { ago, expiryChip, expiryFor, openedLabel, timeLeft } from "./shareFormat";

const NOW = 1_800_000_000;

describe("share link wording (#1404 P4)", () => {
  test("expiry presets ask for an absolute time; never is null; a bad pick is NaN", () => {
    expect(expiryFor("never", NOW)).toBeNull();
    expect(expiryFor("1h", NOW)).toBe(NOW + 3600);
    expect(expiryFor("1d", NOW + 0.7)).toBe(NOW + 86400);
    expect(expiryFor("7d", NOW)).toBe(NOW + 7 * 86400);
    expect(expiryFor("date", NOW, "")).toBeNaN();
    expect(expiryFor("date", NOW, "not a date")).toBeNaN();
    const picked = expiryFor("date", NOW, "2030-01-02T03:04");
    expect(picked).toBe(Math.floor(new Date("2030-01-02T03:04").getTime() / 1000));
  });

  test("time left is coarse and never zero", () => {
    expect(timeLeft(NOW + 10, NOW)).toBe("1 MIN");
    expect(timeLeft(NOW + 40 * 60, NOW)).toBe("40 MIN");
    expect(timeLeft(NOW + 23 * 3600 + 59, NOW)).toBe("23 H");
    expect(timeLeft(NOW + 6 * 86400 + 5, NOW)).toBe("6 D");
    expect(expiryChip(null, NOW)).toBe("NEVER EXPIRES");
    expect(expiryChip(NOW + 6 * 86400, NOW)).toBe("EXPIRES IN 6 D");
  });

  test("opened and ago", () => {
    expect(openedLabel({ open_count: 0, last_opened_at: null }, NOW)).toBe("not opened yet");
    expect(openedLabel({ open_count: 4, last_opened_at: NOW - 2 * 3600 }, NOW)).toBe(
      "opened 4× · last 2 h ago",
    );
    expect(ago(NOW - 30, NOW)).toBe("just now");
    expect(ago(NOW - 300, NOW)).toBe("5 min ago");
    expect(ago(NOW - 30 * 3600, NOW)).toBe("yesterday");
    expect(ago(NOW - 3 * 86400, NOW)).toBe("3 d ago");
  });
});
