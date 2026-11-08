/** An agent's own report in NEEDS YOU (#1882): its words framed as unchecked agent text, its
 *  numbered options (a tap IS the approval and names the option by number), Dismiss, and the
 *  options disabled — with the server's reason and a way to the session — when a tap could only
 *  be refused. */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../lib/api";
import type { NeedsYouPayload, NeedsYouRow } from "../../types/api";
import { NeedsYou } from "./NeedsYou";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: { answerReport: vi.fn(), dismissReport: vi.fn(), approveAction: vi.fn() },
  };
});

const KEY = "claude:aaaaaaaa-0000-4000-8000-00000000000a";
const REASON =
  "Answering from here needs the orchestrator on (Settings → Mission control). Open the session to answer it.";

function row(over: Partial<NeedsYouRow> = {}): NeedsYouRow {
  return {
    id: KEY,
    engine: "claude",
    title: "Move session tokens off the cookie",
    project: { id: "p1", name: "auth-service" },
    last_activity: 1,
    since: Math.floor(Date.now() / 1000) - 180,
    reason: "",
    summary: "",
    flagged: false,
    kind: "report_decision",
    menu: null,
    action: null,
    report: {
      id: "rep1",
      state: "needs_decision",
      summary: "Both token stores pass. <b>not markup</b>",
      question: "Where should session tokens live?",
      options: ["Postgres — the table we already back up", "Redis", "Keep the cookie", "Stop here"],
      at: 1,
    },
    report_answer: { available: true, reason: "" },
    ...over,
  };
}

function view(rows: NeedsYouRow[], onChanged: () => void, onDetails: (id: string, rep?: string | null) => void = () => {}) {
  const data: NeedsYouPayload = {
    rows,
    total: rows.length,
    total_unfiltered: rows.length,
    truncated: false,
    facets: { engines: [], projects: [] },
    window_days: 1,
  };
  return (
    <MemoryRouter>
      <NeedsYou
        state={{ status: "ok", data }}
        facets={data.facets}
        engine=""
        project=""
        onEngine={() => {}}
        onProject={() => {}}
        onDetails={onDetails}
        onChanged={onChanged}
      />
    </MemoryRouter>
  );
}

function renderRows(rows: NeedsYouRow[], onChanged = vi.fn()) {
  render(view(rows, onChanged));
  return onChanged;
}

beforeEach(() => {
  vi.mocked(api.answerReport).mockReset();
  vi.mocked(api.dismissReport).mockReset();
});

test("the report is framed as the agent's own words, rendered as text, with numbered options", () => {
  renderRows([row()]);
  const r = screen.getByTestId("needs-you-row");
  expect(within(r).getByText("The agent asks you to decide")).toBeInTheDocument();
  expect(within(r).getByText(/its own words, unchecked/)).toBeInTheDocument();
  // Agent text is TEXT: a tag in it is shown, never parsed.
  expect(screen.getByTestId("needs-you-report-summary").textContent).toBe(
    "Both token stores pass. <b>not markup</b>",
  );
  expect(r.querySelector("b")).toBeNull();
  expect(screen.getByTestId("needs-you-report-question").textContent).toBe(
    "Where should session tokens live?",
  );
  const opts = screen.getAllByTestId("needs-you-report-option");
  expect(opts.map((o) => o.textContent)).toEqual([
    "1Postgres — the table we already back up",
    "2Redis",
    "3Keep the cookie",
    "4Stop here",
  ]);
  expect(screen.getByText(/A tap sends it/).textContent).toContain("Decision: <option>");
  // The report's options are not the row's Approve: no primary on a report row.
  expect(screen.queryByTestId("needs-you-approve")).toBeNull();
});

test("a tap answers by the option's NUMBER and re-reads the list", async () => {
  vi.mocked(api.answerReport).mockResolvedValue({} as never);
  const onChanged = renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[1]);
  expect(api.answerReport).toHaveBeenCalledWith(KEY, "rep1", 2);
  expect(onChanged).toHaveBeenCalled();
});

test("an UNCERTAIN outcome (attempted, unconfirmed) is never shown as success", async () => {
  vi.mocked(api.answerReport).mockResolvedValue({ state: "indeterminate", outcome: "uncertain" } as never);
  const onChanged = renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/may already have been sent/);
  expect(alert.textContent).not.toMatch(/Nothing was sent/);
  expect(within(alert).getByRole("link", { name: "Open session" })).toBeInTheDocument();
  expect(onChanged).toHaveBeenCalled();
});

test("only a DELIVERED outcome reads as success", async () => {
  vi.mocked(api.answerReport).mockResolvedValue({ state: "delivered", outcome: "delivered" } as never);
  const onChanged = renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  expect(onChanged).toHaveBeenCalled();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a STALE refusal says nothing was sent", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(
    new ApiError(409, "the agent's report was replaced, answered or dismissed", {
      detail: "the agent's report was replaced, answered or dismissed",
      code: "stale",
    }),
  );
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  expect((await screen.findByRole("alert")).textContent).toMatch(/^Nothing was sent/);
});

test("a SPENT refusal never says nothing was sent: it may have been, so check the session", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(
    new ApiError(409, "an answer to this report was already sent, or may have been", {
      detail: "an answer to this report was already sent, or may have been",
      code: "spent",
    }),
  );
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).not.toMatch(/Nothing was sent/);
  expect(alert.textContent).toMatch(/may already have been sent/);
  expect(within(alert).getByRole("link", { name: "Open session" }).getAttribute("href")).toBe(
    "/s/claude/aaaaaaaa-0000-4000-8000-00000000000a",
  );
});

test("an UNKNOWN refusal (the ledger can't be read) claims neither, and points to the session", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(
    new ApiError(503, "the decision ledger can't be read, so an earlier answer can't be ruled out", {
      detail: "the decision ledger can't be read, so an earlier answer can't be ruled out",
      code: "unknown",
    }),
  );
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).not.toMatch(/Nothing was sent|may already have been sent/);
  expect(alert.textContent).toMatch(/can't be ruled out/);
  expect(within(alert).getByRole("link", { name: "Open session" })).toBeInTheDocument();
});

test("an unlabelled 409 is treated as possibly sent: warning, Open session, options stay locked", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(new ApiError(409, "that went wrong"));
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).not.toMatch(/Nothing was sent/);
  expect(alert.textContent).toMatch(/may already have been sent/);
  for (const o of screen.getAllByTestId("needs-you-report-option")) expect(o).toBeDisabled();
});

test("a lost response (the fetch rejects after the server wrote) never re-enables the options", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(new TypeError("Failed to fetch"));
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/may already have been sent/);
  expect(alert.textContent).not.toMatch(/Nothing was sent/);
  expect(within(alert).getByRole("link", { name: "Open session" })).toBeInTheDocument();
  for (const o of screen.getAllByTestId("needs-you-report-option")) expect(o).toBeDisabled();
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[1]);
  expect(api.answerReport).toHaveBeenCalledTimes(1);
});

test("a 5xx without a code is possibly sent too; only an explicit stale gives the options back", async () => {
  vi.mocked(api.answerReport).mockRejectedValueOnce(new ApiError(502, "bad gateway"));
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  expect((await screen.findByRole("alert")).textContent).toMatch(/may already have been sent/);
  for (const o of screen.getAllByTestId("needs-you-report-option")) expect(o).toBeDisabled();
});

test("an explicit STALE refusal gives the options back", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(
    new ApiError(409, "the agent's report was replaced", { detail: "x", code: "stale" }),
  );
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  expect((await screen.findByRole("alert")).textContent).toMatch(/^Nothing was sent/);
  for (const o of screen.getAllByTestId("needs-you-report-option")) expect(o).toBeEnabled();
});

test("an UNCERTAIN refusal (the report's record is no longer held) says it may have been sent", async () => {
  vi.mocked(api.answerReport).mockRejectedValue(
    new ApiError(409, "may have", { detail: "may have", code: "uncertain" }),
  );
  renderRows([row()]);
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/may already have been sent/);
  for (const o of screen.getAllByTestId("needs-you-report-option")) expect(o).toBeDisabled();
});

test("a replaced report starts clean: A's tap in flight neither marks B as sending nor shows its late error", async () => {
  let fail: (e: unknown) => void = () => {};
  vi.mocked(api.answerReport).mockReturnValue(
    new Promise((_resolve, reject) => {
      fail = reject;
    }) as never,
  );
  const onChanged = vi.fn();
  const { rerender } = render(view([row()], onChanged));
  await userEvent.click(screen.getAllByTestId("needs-you-report-option")[0]);
  expect(screen.getAllByTestId("needs-you-report-option")[0].textContent).toContain("Sending…");
  // The list refreshes: the same session now shows the agent's NEWER report B.
  const b = row({
    report: {
      id: "rep-b",
      state: "needs_decision",
      summary: "A newer question.",
      question: "Which region?",
      options: ["eu-west", "us-east"],
      at: 2,
    },
  });
  rerender(view([b], onChanged));
  const opts = screen.getAllByTestId("needs-you-report-option");
  expect(opts.map((o) => o.textContent)).toEqual(["1eu-west", "2us-east"]);
  for (const o of opts) expect(o).toBeEnabled();
  expect(screen.queryByText("Sending…")).toBeNull();
  // A's tap now fails late: nothing of it reaches B.
  await act(async () => {
    fail(new ApiError(409, "late", { detail: "late", code: "spent" }));
  });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getAllByTestId("needs-you-report-option")[0]).toBeEnabled();
});

test("the report row's button says it dismisses the REPORT", () => {
  renderRows([row()]);
  expect(screen.getByTestId("needs-you-report-dismiss").textContent).toBe("Dismiss report");
});

test("Dismiss ends the report and sends nothing", async () => {
  vi.mocked(api.dismissReport).mockResolvedValue({ dismissed: true });
  const onChanged = renderRows([row()]);
  await userEvent.click(screen.getByTestId("needs-you-report-dismiss"));
  expect(api.dismissReport).toHaveBeenCalledWith(KEY, "rep1");
  expect(api.answerReport).not.toHaveBeenCalled();
  expect(onChanged).toHaveBeenCalled();
});

test("when delivery is impossible the options are disabled with the reason and Open session", async () => {
  renderRows([row({ report_answer: { available: false, reason: REASON } })]);
  const opts = screen.getAllByTestId("needs-you-report-option");
  for (const o of opts) expect(o).toBeDisabled();
  const reason = screen.getByTestId("needs-you-report-reason");
  expect(reason.textContent).toContain(REASON);
  expect(opts[0].getAttribute("aria-describedby")).toBe(reason.id);
  const open = within(reason).getByRole("link", { name: "Open session" });
  expect(open.getAttribute("href")).toBe(
    "/s/claude/aaaaaaaa-0000-4000-8000-00000000000a",
  );
  await userEvent.click(opts[0]);
  expect(api.answerReport).not.toHaveBeenCalled();
  // Dismiss still works: it sends nothing.
  expect(screen.getByTestId("needs-you-report-dismiss")).toBeEnabled();
});

test("a blocked report has no options, only its words and Dismiss", () => {
  renderRows([
    row({
      kind: "report_blocked",
      report: { id: "rep2", state: "blocked", summary: "CI fails at the same step.", at: 1 },
    }),
  ]);
  expect(screen.getByText("The agent says it's blocked")).toBeInTheDocument();
  expect(screen.queryAllByTestId("needs-you-report-option")).toHaveLength(0);
  expect(screen.getByTestId("needs-you-report-dismiss")).toBeInTheDocument();
});

test("focus order: title, the options in order, Dismiss, details", async () => {
  renderRows([row()]);
  const user = userEvent.setup();
  const seen: string[] = [];
  for (let i = 0; i < 9; i++) {
    await user.tab();
    const el = document.activeElement as HTMLElement;
    seen.push(el.dataset.testid ?? el.getAttribute("aria-label") ?? el.textContent ?? "");
  }
  const start = seen.indexOf("Move session tokens off the cookie");
  expect(seen.slice(start, start + 7)).toEqual([
    "Move session tokens off the cookie",
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-dismiss",
    "needs-you-details",
  ]);
});

test("Details carries the report the row DISPLAYS, so its dismissal can name only that one", async () => {
  const onDetails = vi.fn();
  render(view([row()], vi.fn(), onDetails));
  await userEvent.click(screen.getByTestId("needs-you-details"));
  expect(onDetails).toHaveBeenCalledWith(KEY, "rep1");
});
