import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AgentUsageRow } from "../../types/api";
import { accountName, firstToRunOut, otherWindows, showsAccount } from "../../lib/agentUsage";
import { UsageAccountHead, UsageWindows } from "./UsageWindows";

// Every quota window of one account (#2331): the rules that decide which window is marked and
// whose name a row carries. Layout is pinned in the real browser (e2e/agent-account-usage.spec.ts).

const row: AgentUsageRow = {
  engine: "claude",
  account: { id: "a-1", label: "Personal", email: "op@example.com" },
  source: "plan",
  windows: [
    { label: "session", used_pct: 51, resets_at: null },
    { label: "week", used_pct: 13, resets_at: null },
  ],
  plan: "max_20x",
  at: 1,
  checked_at: 1,
  stale: false,
  limit_tokens: 0,
  manual_used: 0,
  used_pct: 51,
};

describe("which window runs out first", () => {
  it("is the fullest window when nothing runs out sooner", () => {
    expect(firstToRunOut(row)).toBe("session");
  });
  it("is the forecast's window when one runs out before its reset", () => {
    const f = { state: "exhausts" as const, window: "week", resets_at: 2, runs_out_at: 1, pct_at_reset: null, rate_per_h: 1 };
    expect(firstToRunOut({ ...row, forecast: f })).toBe("week");
    expect(firstToRunOut({ ...row, forecast: { ...f, state: "ok" } })).toBe("session");
  });
});

describe("whose quota", () => {
  it("names the email, else the label, else nothing", () => {
    expect(accountName(row)).toBe("op@example.com");
    expect(accountName({ ...row, account: { id: "a-1", label: "Personal", email: null } })).toBe("Personal");
    expect(accountName({ ...row, account: null })).toBe("");
  });
  it("names a default login only when the vendor said who it is", () => {
    expect(showsAccount({ ...row, account: { id: "default", label: "Default login", email: null } })).toBe(false);
    expect(showsAccount({ ...row, account: { id: "default", label: "Default login", email: "x@y.z" } })).toBe(true);
    expect(showsAccount(row)).toBe(true);
  });
  it("lists the windows the headline does not name", () => {
    expect(otherWindows(row).map((w) => w.label)).toEqual(["week"]);
  });
});

describe("UsageAccountHead", () => {
  it("shows the account's own name, then the email and the plan", () => {
    render(<UsageAccountHead row={row} pooled />);
    const head = screen.getByTestId("usage-account");
    expect(head).toHaveTextContent("Personal · op@example.com · max_20x plan");
    expect(head).toHaveTextContent("New sessions start here");
  });
  it("on an agent's only login, the email stands alone", () => {
    render(
      <UsageAccountHead
        row={{ ...row, account: { id: "default", label: "Default login", email: "op@example.com" }, plan: null }}
        pooled={false}
      />,
    );
    expect(screen.getByTestId("usage-account")).toHaveTextContent(/^op@example\.com$/);
  });
  it("says nothing when there is nothing to name", () => {
    const { container } = render(
      <UsageAccountHead row={{ ...row, account: { id: "default", label: "Default login", email: null }, plan: null }} pooled={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("UsageWindows", () => {
  it("draws one meter per window and marks the first to run out", () => {
    render(<UsageWindows row={row} threshold={90} />);
    expect(screen.getAllByRole("meter")).toHaveLength(2);
    expect(screen.getByText("session").closest("[data-window]")).toHaveAttribute("data-first", "true");
  });
  it("an account not asked yet is said so, never drawn as 0 %", () => {
    render(<UsageWindows row={{ ...row, windows: [], at: 0, used_pct: null }} threshold={90} />);
    expect(screen.getByTestId("usage-windows-none")).toHaveTextContent("Not asked yet");
    expect(screen.queryByRole("meter")).toBeNull();
  });
  it("a failed first probe shows why", () => {
    render(<UsageWindows row={{ ...row, windows: [], error: "not logged in" }} threshold={90} />);
    expect(screen.getByTestId("usage-windows-none")).toHaveTextContent("No figures yet — not logged in");
  });
});
