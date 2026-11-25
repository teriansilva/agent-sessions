/** The plan & tools rail (#1726): what it shows from the normalized snapshot, its honest empty
 *  and unavailable states, and a status word beside every mark. Layout, scroll and motion are
 *  proved in a real browser (`e2e/structured-api-rail.spec.ts`). */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import type { StructuredSnapshot } from "../../types/api";
import { ApiRail, type RailTab } from "./ApiRail";

type Native = NonNullable<StructuredSnapshot["native"]>;

function snap(native: Partial<Native>): StructuredSnapshot {
  return {
    session_key: "claude-api:x",
    revision: 1,
    event_cursor: 1,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false, ...native },
  } as StructuredSnapshot;
}

const COUNTS = {
  tools: [
    { name: "Bash", calls: 14 },
    { name: "Read", calls: 22 },
  ],
  tools_other: 0,
  servers: [{ name: "forgejo", calls: 11 }],
  servers_other: 0,
  unattributed: 2,
  skills: [{ name: "code-review", calls: 1 }],
  skills_other: 0,
};

function rail(native: Partial<Native>, tab: RailTab = "plan", extra: { mode?: "dock" | "sheet"; onClose?: () => void } = {}) {
  return render(
    <ApiRail
      snap={snap(native)}
      mode={extra.mode ?? "dock"}
      width={340}
      tab={tab}
      onTab={() => {}}
      onClose={extra.onClose ?? (() => {})}
    />,
  );
}

test("no plan shared yet is said, not shown as an empty checklist", () => {
  rail({ plan: null });
  expect(screen.getByTestId("api-rail-empty")).toHaveTextContent("No plan yet");
  expect(screen.queryByTestId("api-rail-plan")).toBeNull();
  expect(screen.getByRole("tab", { name: "Plan" })).toBeInTheDocument();
});

test("every checklist row carries its state as a word, and the document is a closed disclosure", () => {
  rail({
    plan: {
      entries: [
        { text: "Reproduce the gap", status: "completed" },
        { text: "Keep facets before filters", status: "in_progress" },
        { text: "Update the docs", status: "pending" },
      ],
      explanation: "Fix the order first.",
      entries_at: 1_790_000_000,
      document: "# The plan\n\n1. one\n2. two",
      document_at: 1_790_000_100,
    },
  });
  const rows = screen.getAllByTestId("api-rail-plan-entry");
  expect(rows.map((r) => r.getAttribute("data-status"))).toEqual(["completed", "in_progress", "pending"]);
  expect(rows[0]).toHaveTextContent("Done");
  expect(rows[1]).toHaveTextContent("In progress");
  expect(rows[2]).toHaveTextContent("Pending");
  expect(screen.getByRole("tab", { name: "Plan 1/3" })).toBeInTheDocument();
  expect(screen.getByText("Fix the order first.")).toBeInTheDocument();
  const doc = screen.getByTestId("api-rail-plan-document");
  expect(within(doc).getByRole("button")).toHaveAttribute("aria-expanded", "false");
  expect(within(doc).getByRole("button")).toHaveTextContent("4 lines");
});

test("a measured server has an LED and its word; a configured one has no LED, never reads healthy", () => {
  rail(
    {
      environment: {
        skill_use: true,
        mcp: [
          { name: "forgejo", status: "connected" },
          { name: "nextcloud", status: "failed" },
          { name: "github", status: "configured" },
        ],
        skills: [{ name: "code-review" }, { name: "simplify" }],
        commands: [{ name: "compact" }, { name: "review" }],
      },
      counts: COUNTS,
    },
    "tools",
  );
  const servers = screen.getAllByTestId("api-rail-mcp-server");
  expect(servers[0]).toHaveTextContent(/forgejo.*Connected.*11/);
  expect(servers[1]).toHaveTextContent(/nextcloud.*Failed/);
  expect(servers[2]).toHaveTextContent(/github.*Configured/);
  expect(servers[2]).not.toHaveTextContent("Connected");
  // The LED is a status-coloured round mark; a configured server gets the hollow square only.
  expect(servers[0].querySelector('[class*="led"]')).not.toBeNull();
  expect(servers[2].querySelector('[class*="led"]')).toBeNull();
  expect(screen.getByTestId("api-rail-unattributed")).toHaveTextContent("2");
  const skills = screen.getAllByTestId("api-rail-skill");
  expect(skills[0]).toHaveTextContent("Used this session");
  expect(skills[1]).not.toHaveTextContent("Used this session");
  expect(screen.getByTestId("api-rail-commands")).toHaveTextContent("/compact");
  const counts = screen.getAllByTestId("api-rail-tool-count");
  expect(counts.map((c) => c.textContent)).toEqual(["Bash14", "Read22"]);
  expect(screen.getByTestId("api-rail-tool-counts")).toHaveTextContent("36 calls");
});

test("a list the client never reported says so; a client without reported skill use says that", () => {
  rail({ environment: { skill_use: false, mcp: [{ name: "fixture", status: "connected" }] }, counts: COUNTS }, "tools");
  expect(screen.getByTestId("api-rail-commands")).toHaveTextContent("Not reported by this client");
  expect(screen.getByTestId("api-rail-skills")).toHaveTextContent("doesn’t report using a skill");
});

test("with no live worker the tools are not running, and the conversation's counts still show", () => {
  rail({ worker: null, environment: null, counts: COUNTS }, "tools");
  expect(screen.getByTestId("api-rail-empty")).toHaveTextContent("Not running");
  expect(screen.getAllByTestId("api-rail-tool-count")).toHaveLength(2);
});

test("a sheet is a modal dialog: focus moves in, Escape and the scrim close it", async () => {
  const onClose = vi.fn();
  rail({ plan: null }, "plan", { mode: "sheet", onClose });
  const dialog = screen.getByRole("dialog", { name: "Plan and tools" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(screen.getByRole("tab", { name: "Plan" })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a checklist that may be out of date never reads Live (review on #1811)", () => {
  rail({ plan: { entries: [{ text: "A", status: "pending" }], entries_at: 1, stale: true } });
  expect(screen.getByTestId("api-rail-plan-state")).toHaveTextContent("Saved — may be out of date");
});

test("a live, confirmed checklist reads Live", () => {
  rail({ plan: { entries: [{ text: "A", status: "pending" }], entries_at: 1, stale: false } });
  expect(screen.getByTestId("api-rail-plan-state")).toHaveTextContent("Live");
});

test("a plan document older than the newest checklist is labelled from earlier, never current", () => {
  rail({
    plan: {
      entries: [{ text: "A", status: "pending" }],
      entries_at: 200,
      document: "# Old plan",
      document_at: 100,
    },
  });
  expect(screen.getByTestId("api-rail-document-earlier")).toHaveTextContent("From earlier in the session");
  rail({ plan: { entries: [], entries_at: 100, document: "# New", document_at: 200 } });
  expect(screen.getAllByTestId("api-rail-document-earlier")).toHaveLength(1);
});

test("a replacement worker reporting other servers keeps the conversation's totals (Hermes on #1811)", () => {
  const counts = { ...COUNTS, servers: [{ name: "forgejo", calls: 11 }], servers_other: 3, skills_other: 2 };
  // Worker A reported forgejo; its successor B reports only github.
  rail({ environment: { skill_use: true, mcp: [{ name: "github", status: "connected" }] }, counts }, "tools");
  const earlier = screen.getByTestId("api-rail-mcp-earlier");
  expect(earlier).toHaveTextContent("forgejo");
  expect(earlier).toHaveTextContent("Called earlier in this session");
  expect(earlier).toHaveTextContent("11");
  expect(screen.getByTestId("api-rail-mcp-extra")).toHaveTextContent("Other servers");
  expect(screen.getByTestId("api-rail-skills")).toHaveTextContent("2 uses of other skills");
});
