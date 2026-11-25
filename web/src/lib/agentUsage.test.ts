import { describe, expect, it } from "vitest";
import {
  billable,
  quotaReadable,
  shortTokens,
  stalenessNote,
  tone,
  usageCaption,
  worstWindow,
} from "./agentUsage";
import type { AgentUsageRow } from "../types/api";

const base: AgentUsageRow = {
  engine: "claude",
  source: "plan",
  at: 1_800_000_000,
  checked_at: 1_800_000_000,
  stale: false,
  limit_tokens: 0,
  manual_used: 0,
  used_pct: null,
};

describe("shortTokens", () => {
  it("keeps nine-figure counts readable", () => {
    expect(shortTokens(104_391_103)).toBe("104M");
    expect(shortTokens(9_600_000)).toBe("9.6M");
    expect(shortTokens(723_597)).toBe("724k");
    expect(shortTokens(42)).toBe("42");
  });

  it("does not render a missing count as zero", () => {
    expect(shortTokens(NaN)).toBe("—");
    expect(shortTokens(-1)).toBe("—");
  });
});

describe("billable", () => {
  it("excludes cache reads, matching the server", () => {
    // Cache reads dominate the raw total, so including them would show a limit as breached
    // for a reason the operator cannot act on.
    expect(
      billable({
        ...base,
        source: "tokens",
        tokens: { in: 100, out: 5, cache_read: 10_000, cache_write: 40 },
      }),
    ).toBe(105);
  });

  it("counts the operator's own number for a manual agent", () => {
    expect(
      billable({
        ...base,
        source: "manual",
        manual_used: 250,
        tokens: { in: 9_000_000, out: 0 },
      }),
    ).toBe(250);
  });
});

describe("worstWindow", () => {
  it("picks the window nearest its limit, not the first one", () => {
    const row: AgentUsageRow = {
      ...base,
      windows: [
        { label: "session", used_pct: 5, resets_at: null },
        { label: "week", used_pct: 96, resets_at: null },
        { label: "week (Fable)", used_pct: 0, resets_at: null },
      ],
    };
    expect(worstWindow(row)?.label).toBe("week");
  });
});

describe("tone", () => {
  it("separates room, warning and over", () => {
    expect(tone(10, 90)).toBe("ok");
    expect(tone(90, 90)).toBe("warn");
    expect(tone(100, 90)).toBe("over");
  });

  it("never paints an unmeasured agent green", () => {
    // "nothing reported" is not "0% used", and a green bar would say it is.
    expect(tone(null, 90)).toBe("none");
  });
});

describe("usageCaption", () => {
  it("names the window and reset for a plan agent", () => {
    const row: AgentUsageRow = {
      ...base,
      plan: "max",
      windows: [
        { label: "session", used_pct: 5, resets_at: null },
        { label: "week (all models)", used_pct: 31, resets_at: 1_800_000_000 },
      ],
    };
    const text = usageCaption(row);
    expect(text).toContain("week (all models)");
    expect(text).toContain("max plan");
  });

  it("says the count is the operator's own for a manual agent", () => {
    const text = usageCaption({
      ...base,
      engine: "kimi",
      source: "manual",
      manual_used: 400_000,
      limit_tokens: 1_000_000,
    });
    // A number the operator typed and a quota the agent reported are not the same claim.
    expect(text).toContain("counted by you");
    expect(text).toContain("400k of 1M");
  });

  it("tells the operator what is missing when there is no limit", () => {
    const text = usageCaption({
      ...base,
      engine: "opencode",
      source: "tokens",
      tokens: { in: 5_000_000, out: 0 },
      window_days: 7,
    });
    expect(text).toContain("set a limit");
  });

  it("does not present an unmeasured agent as idle", () => {
    const text = usageCaption({ ...base, engine: "gemini", source: "none" });
    expect(text).toContain("reports no usage");
    expect(text).not.toContain("0 tokens");
  });
});

describe("stalenessNote", () => {
  const now = 1_800_000_000;

  it("is silent when the figure is current", () => {
    expect(stalenessNote({ ...base, at: now - 60, stale: false }, now)).toBe(
      "",
    );
  });

  it("dates a stale figure rather than hiding it", () => {
    expect(stalenessNote({ ...base, at: now - 7200, stale: true }, now)).toBe(
      "2h ago",
    );
  });

  it("says a probe failed AND that the figures are the last good ones", () => {
    const note = stalenessNote(
      { ...base, at: now - 600, stale: false, error: "network unreachable" },
      now,
    );
    expect(note).toContain("last good figures");
    expect(note).toContain("network unreachable");
  });

  it("distinguishes never-asked from stale", () => {
    expect(stalenessNote({ ...base, at: 0 }, now)).toBe("not asked yet");
  });

  it("says nothing about staleness for a number the operator maintains", () => {
    // A manual counter has no probe behind it; "2h ago" would be meaningless.
    expect(stalenessNote({ ...base, source: "manual", at: 0 }, now)).toBe("");
  });
});

describe("quotaReadable — what the dashboard's quota tile lists", () => {
  const week = [{ label: "week", used_pct: 40, resets_at: null }];
  it("lists a current plan quota", () => {
    expect(quotaReadable({ ...base, windows: week })).toBe(true);
  });
  it("leaves out an agent nobody configured", () => {
    expect(quotaReadable({ ...base, source: "none" })).toBe(false);
    expect(
      quotaReadable({ ...base, source: "tokens", tokens: { in: 5, out: 1 } }),
    ).toBe(false);
    expect(quotaReadable({ ...base, source: "manual", manual_used: 3 })).toBe(
      false,
    );
  });
  it("lists a count once it has a limit", () => {
    expect(
      quotaReadable({
        ...base,
        source: "tokens",
        tokens: { in: 5, out: 1 },
        limit_tokens: 100,
      }),
    ).toBe(true);
    expect(
      quotaReadable({ ...base, source: "manual", limit_tokens: 100 }),
    ).toBe(true);
  });
  it("leaves out a quota that could not be read", () => {
    expect(quotaReadable({ ...base, windows: [] })).toBe(false);
    expect(quotaReadable({ ...base, windows: week, stale: true })).toBe(false);
    expect(
      quotaReadable({
        ...base,
        windows: week,
        access: {
          state: "denied",
          message: "x",
          observed_at: 1,
          checked_at: 1,
        },
      }),
    ).toBe(false);
    expect(
      quotaReadable({
        ...base,
        source: "tokens",
        limit_tokens: 100,
        error: "locked",
      }),
    ).toBe(false);
  });
  it("keeps current figures that came with a probe error (a fallback's reading)", () => {
    expect(
      quotaReadable({ ...base, windows: week, error: "app-server timed out" }),
    ).toBe(true);
  });
});
