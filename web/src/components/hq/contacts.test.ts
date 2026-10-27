import { existsSync, readFileSync, statSync } from "node:fs";

import { describe, expect, test, vi } from "vitest";

import {
  CONTACTS,
  DEFAULT_HQ_CHOICE,
  PERM,
  coerceHqChoice,
  contactForDay,
  localDayNumber,
  msToLocalMidnight,
  resolveHqContact,
} from "./contacts";

const day = (y: number, m: number, d: number, h = 12) => new Date(y, m - 1, d, h);

describe("Random: a 13-day cycle (#1525)", () => {
  test("PERM is a permutation of every contact", () => {
    expect([...PERM].sort()).toEqual(CONTACTS.map((c) => c.id).sort());
  });

  test("the same contact all day, midnight to midnight", () => {
    const morning = contactForDay(day(2026, 10, 20, 0));
    expect(contactForDay(day(2026, 10, 20, 12))).toBe(morning);
    expect(contactForDay(new Date(2026, 9, 20, 23, 59, 59))).toBe(morning);
  });

  test("a different contact on every pair of consecutive days, cycle boundary included", () => {
    for (let i = 0; i < 400; i++) {
      const a = contactForDay(day(2026, 1, 1 + i));
      const b = contactForDay(day(2026, 1, 2 + i));
      expect(b.id, `day ${i} → ${i + 1}`).not.toBe(a.id);
    }
  });

  test("every contact exactly once in any 13 consecutive days", () => {
    for (let start = 0; start < 60; start++) {
      const ids = Array.from({ length: 13 }, (_, i) => contactForDay(day(2026, 3, 1 + start + i)).id);
      expect(new Set(ids).size).toBe(13);
    }
  });

  test("the day number counts LOCAL calendar days", () => {
    expect(localDayNumber(day(1970, 1, 1, 0))).toBe(0);
    expect(localDayNumber(day(1970, 1, 2, 23))).toBe(1);
  });

  test("the next switch is at local midnight", () => {
    expect(msToLocalMidnight(new Date(2026, 9, 20, 23, 59, 0))).toBe(60_000);
  });
});

describe("the stored choice", () => {
  test("an unknown or removed id, or garbage, reads as the default", () => {
    for (const v of ["general-nobody", "", 42, null, undefined, { id: "us-man" }]) {
      expect(coerceHqChoice(v)).toBe(DEFAULT_HQ_CHOICE);
    }
    expect(DEFAULT_HQ_CHOICE).toBe("random");
  });

  test("a known id, random and off are kept", () => {
    expect(coerceHqChoice("romania-woman")).toBe("romania-woman");
    expect(coerceHqChoice("random")).toBe("random");
    expect(coerceHqChoice("off")).toBe("off");
  });

  test("off shows no one; a contact shows itself whatever the day", () => {
    expect(resolveHqContact("off", day(2026, 10, 20))).toBeNull();
    expect(resolveHqContact("us-man", day(2026, 10, 20))?.name).toBe("General Jack Harlan");
    expect(resolveHqContact("random", day(2026, 10, 20))).toBe(contactForDay(day(2026, 10, 20)));
  });

  test("the outlaw has her name, and her region reads Romania", () => {
    const lupu = CONTACTS.find((c) => c.id === "romania-woman")!;
    expect(lupu.name).toBe("General Ileana Lupu");
    expect(lupu.region).toBe("Romania");
  });
});

describe("the assets", () => {
  test("every contact has its loop pair and poster, within budget", () => {
    for (const c of CONTACTS) {
      for (const url of [c.webm, c.mp4, c.poster]) {
        const file = `public${url}`;
        expect(existsSync(file), file).toBe(true);
        expect(statSync(file).size, file).toBeLessThanOrEqual(400 * 1024);
      }
    }
  });

  test("the URLs sit under the build's base: the relay serves the SPA from /connect/", async () => {
    vi.stubEnv("BASE_URL", "/connect/");
    vi.resetModules();
    try {
      const { CONTACTS: under } = await import("./contacts");
      for (const c of under) {
        expect(c.poster).toBe(`/connect/hq/${c.id}.jpg`);
        expect(c.webm).toBe(`/connect/hq/${c.id}.webm`);
        expect(c.mp4).toBe(`/connect/hq/${c.id}.mp4`);
      }
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });

  test("the loops are never precached: only the chosen contact's loads, on first open", () => {
    const cfg = readFileSync("vite.config.ts", "utf8");
    const m = /globPatterns:\s*\[([^\]]*)\]/.exec(cfg);
    expect(m).not.toBeNull();
    for (const ext of ["webm", "mp4", "jpg"]) expect(m![1]).not.toMatch(new RegExp(`\\b${ext}\\b`));
  });
});
