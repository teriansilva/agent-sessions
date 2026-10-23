/** The decision row (`ActionRow`, #726 Phases 1–2), as a mission's console renders it.
 *
 * Pinned: only a DELIVERING verb offers approve/reject, so the UI never implies an escalation would
 * be sent; a 409 from compare-and-execute reads as "nothing was sent", distinguishable from an
 * error; evidence is fetched from the server on expand rather than rendered from anything the model
 * said; and every row names its project. The autonomy strip these tests once rendered beside the
 * rows (`Orchestrator.tsx`) was unmounted by #930 and deleted in #1531 Phase 14; its health line
 * lives on as `OrchestratorHealth` (its own tests), and the tier as Settings → Orchestrator.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { ActionRow } from "./ActionRow";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import type { OrchestratorAction, OrchestratorConfig } from "../../types/api";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      orchestrator: vi.fn(),
      evidence: vi.fn(),
      setPrefs: vi.fn(),
      approveAction: vi.fn(),
      rejectAction: vi.fn(),
    },
  };
});

function config(over: Partial<OrchestratorConfig> = {}): OrchestratorConfig {
  return {
    enabled: true,
    autonomy: "suggest",
    allowed_verbs: ["continue"],
    auto_verbs_ceiling: ["continue"],
    confidence_min: 0.75,
    interval_minutes: 10,
    max_actions_per_pass: 4,
    proposal_ttl_minutes: 30,
    nudge_template: "carry on",
    notify: "escalations",
    configured: true,
    default_nudge_template: "carry on",
    ...over,
  };
}

function action(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "a1",
    state: "proposed",
    ts: Date.now() / 1000,
    tier: "suggest",
    session_id: "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    engine: "claude",
    title: "Kimi transcript adapter",
    project: "agent-sessions",
    project_id: "p1",
    verb: "continue",
    confidence: 0.86,
    rationale: "stopped without running the tests it planned",
    evidence: "screen",
    ...over,
  };
}

/** Mirrors the page's composition after #754: the panel owns tier/threshold/feed, and each
 *  pending action rides on the session card it belongs to. These tests are about what an action
 *  DOES — approve, reject, 409, settle — so they follow the controls to their new home rather
 *  than asserting where they are drawn. */
function PanelAndCards() {
  const [pending, setPending] = useState<OrchestratorAction[]>([]);
  const [resolved, setResolved] = useState<OrchestratorAction[]>([]);
  // The page owns this surface: a 409 carrying a settled record removes the row, so its
  // explanation has to outlive the thing that produced it.
  const [pageNote, setPageNote] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api
      .orchestrator()
      .then((s) => live && setPending(s.pending))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return (
    <>
      {pending.map((a) => (
        <ActionRow
          key={a.id}
          action={a}
          onNote={setPageNote}
          onResolved={(settled) => {
            // The page re-fetches the overview, so a settled action stops being attached to its
            // card. Modelled here by dropping it, and the settled record is surfaced so the
            // test can assert what came back rather than where it was drawn.
            setResolved((r) => [...r, settled]);
            setPending((p) => p.filter((x) => x.id !== settled.id));
          }}
        />
      ))}
      {pageNote && <p>{pageNote}</p>}
      {resolved.map((a) => (
        <span key={`r-${a.id}`} data-testid="settled">
          {a.state}
        </span>
      ))}
    </>
  );
}

function renderIt() {
  return render(
    <MemoryRouter>
      <PanelAndCards />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.orchestrator).mockReset();
  vi.mocked(api.evidence).mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
  vi.mocked(api.approveAction).mockReset();
  vi.mocked(api.rejectAction).mockReset();
});

test("a deliverable proposal offers approve/reject; a decision-only one does not", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [
      action(),
      action({ id: "a2", verb: "escalate", state: "escalated" }),
    ],
    feed: [],
    expired_now: 0,
  });
  renderIt();
  expect(await screen.findAllByRole("listitem")).toHaveLength(2);
  // `continue` delivers, so it can be approved; `escalate` never reaches a session, so it
  // must NOT offer a button that implies it would.
  expect(screen.getAllByRole("button", { name: /^approve$/i })).toHaveLength(1);
});

test("approving delivers and folds the settled record back in place", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
  });
  vi.mocked(api.approveAction).mockResolvedValue(
    action({ state: "delivered" }),
  );
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /^approve$/i }),
  );
  expect(api.approveAction).toHaveBeenCalledWith("a1");
  // Settled → it leaves the pending block rather than lingering as still-actionable.
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /^approve$/i }),
    ).not.toBeInTheDocument(),
  );
  // ...and ARRIVES in Activity. `pending` and `feed` are disjoint by server contract (#726),
  // so a settled row is absent from feed and a `map` had nothing to update — the action
  // vanished from both lists and only reappeared on a refresh, which reads as work lost.
  await waitFor(() =>
    expect(screen.getByText(/delivered/i)).toBeInTheDocument(),
  );
});

test("a rejected action also lands in Activity rather than vanishing", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
  });
  vi.mocked(api.rejectAction).mockResolvedValue(action({ state: "rejected" }));
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /reject this action/i }),
  );
  await waitFor(() =>
    expect(screen.getByText(/rejected/i)).toBeInTheDocument(),
  );
});

test("dismissing an escalation lands it in Activity too", async () => {
  // An escalation has no delivering verb, so it is dismissable but not approvable.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action({ state: "escalated", verb: "escalate" })],
    feed: [],
    expired_now: 0,
  });
  vi.mocked(api.rejectAction).mockResolvedValue(
    action({ state: "rejected", verb: "escalate" }),
  );
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /dismiss this escalation/i }),
  );
  await waitFor(() =>
    expect(screen.getByText(/rejected/i)).toBeInTheDocument(),
  );
});

test("a 409 from compare-and-execute says nothing was sent, and is not an error", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
  });
  const err = new ApiError(
    409,
    "the session's screen changed since this was proposed",
  );
  vi.mocked(api.approveAction).mockRejectedValue(err);
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /^approve$/i }),
  );
  // The operator must be able to tell "nothing happened" from "something went wrong".
  await waitFor(() =>
    expect(
      screen.getByText(/not sent — the session's screen changed/i),
    ).toBeInTheDocument(),
  );
});

test("rejecting settles the action without delivering anything", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
  });
  vi.mocked(api.rejectAction).mockResolvedValue(action({ state: "rejected" }));
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /reject this action/i }),
  );
  expect(api.rejectAction).toHaveBeenCalledWith("a1");
  expect(api.approveAction).not.toHaveBeenCalled();
});

/** Render an `ActionRow` directly. Since #777 the panel has no feed of its own — a session's
 *  action rides its card — so the row's own behaviour is exercised here rather than through a
 *  list that no longer exists. */
function renderRow(over: Partial<OrchestratorAction> = {}) {
  return render(
    <MemoryRouter>
      <ActionRow action={action(over)} embedded />
    </MemoryRouter>,
  );
}

test("evidence is fetched from the server on expand, never rendered from the model", async () => {
  vi.mocked(api.evidence).mockResolvedValue({
    kind: "screen",
    text: "› waiting for input",
    available: true,
  });
  renderRow();
  const toggle = await screen.findByRole("button", {
    name: /show live screen/i,
  });
  // Nothing is fetched until the operator asks for it.
  expect(api.evidence).not.toHaveBeenCalled();
  await userEvent.click(toggle);
  await waitFor(() =>
    expect(screen.getByText(/waiting for input/)).toBeInTheDocument(),
  );
  expect(api.evidence).toHaveBeenCalledWith(
    "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "screen",
  );
});

test("a non-embedded row names its project", async () => {
  // The feed used to group by project under headings; #754 flattened it and moved the project
  // onto the row. #777 then removed the feed entirely, so this is now the row's own contract
  // rather than a statement about a grid — kept because the standalone row still supports it.
  render(
    <MemoryRouter>
      <ActionRow action={action({ project: "agent-sessions" })} />
    </MemoryRouter>,
  );
  expect(screen.getByText("agent-sessions")).toBeInTheDocument();
});

test("evidence is re-fetched on every open — 'live' must not mean 'cached once'", async () => {
  // The server serves evidence uncached so the operator always reads the CURRENT screen.
  // Caching the first snapshot client-side quietly defeats that, and shows a screen the
  // session has moved past — exactly what must not be approved against.
  vi.mocked(api.evidence)
    .mockResolvedValueOnce({
      kind: "screen",
      text: "first screen",
      available: true,
    })
    .mockResolvedValueOnce({
      kind: "screen",
      text: "second screen",
      available: true,
    });
  renderRow();
  const toggle = await screen.findByRole("button", { name: /live screen/i });
  await userEvent.click(toggle);
  await waitFor(() =>
    expect(screen.getByText(/first screen/)).toBeInTheDocument(),
  );
  await userEvent.click(toggle); // collapse
  await userEvent.click(toggle); // re-open
  await waitFor(() =>
    expect(screen.getByText(/second screen/)).toBeInTheDocument(),
  );
  expect(api.evidence).toHaveBeenCalledTimes(2);
});

test("a verb the actuator cannot render offers no Approve", async () => {
  // `dispatch` is a legal verb the model may emit, but `render()` cannot turn it into bytes,
  // so approving it always 409'd. The client used to keep its own DELIVERING set that included
  // it; the server now ships the renderable set so the two cannot drift.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action({ verb: "dispatch" })],
    feed: [],
    expired_now: 0,
    delivering_verbs: ["continue", "choose", "answer"],
  });
  renderIt();
  // The row renders and stays dismissable...
  expect(
    await screen.findByRole("button", { name: /reject this action/i }),
  ).toBeInTheDocument();
  // ...but Approve is not offered for something the server would refuse.
  expect(
    screen.queryByRole("button", { name: /^approve$/i }),
  ).not.toBeInTheDocument();
});

test("a 409 moves the row out of pending instead of leaving it re-clickable", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
    delivering_verbs: ["continue", "choose", "answer"],
  });
  // The server settles the record and returns it WITH the 409 — the client must consume it.
  const settled = action({ state: "stale" });
  vi.mocked(api.approveAction).mockRejectedValue(
    new ApiError(
      409,
      "the session's screen changed since this was proposed",
      settled,
    ),
  );
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /^approve$/i }),
  );

  await waitFor(() =>
    expect(screen.getByText(/not sent/i)).toBeInTheDocument(),
  );
  // The row must leave "Needs a decision" — otherwise every retry 409s again until a refresh.
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /^approve$/i }),
    ).not.toBeInTheDocument(),
  );
  // ...and the server's settled verdict shows up in Activity.
  await waitFor(() => expect(screen.getByText(/stale/i)).toBeInTheDocument());
});

// --- #762 review round 4 --------------------------------------------------------------------

test("an embedded row is not a list item — a Pulse card already is one", async () => {
  // The card is an `<li>`; a nested one produced `<ul><li class=card><li class=act>…`, which
  // is invalid and exposes the action to assistive technology as a second, parentless list
  // item. Standalone (the queue) it IS a list row, so the element depends on placement.
  const { container, rerender } = render(
    <MemoryRouter>
      <ul>
        <li>
          <ActionRow action={action()} embedded />
        </li>
      </ul>
    </MemoryRouter>,
  );
  expect(container.querySelectorAll("li")).toHaveLength(1);
  expect(screen.getAllByRole("listitem")).toHaveLength(1);

  rerender(
    <MemoryRouter>
      <ul>
        <ActionRow action={action()} />
      </ul>
    </MemoryRouter>,
  );
  expect(container.querySelectorAll("li")).toHaveLength(1);
});

test("a collapsed row says how many actions it stands for", async () => {
  // One row per session (#774) — a row that folded in 10 others must not look like the only
  // thing that happened. Since #777 the panel has no feed, so this is the ROW's contract; the
  // card's own history line is covered in Pulse.test.tsx.
  render(
    <MemoryRouter>
      <ActionRow action={action({ id: "a1", repeats: 11 })} />
    </MemoryRouter>,
  );
  expect(screen.getByText("×11")).toBeInTheDocument();
});

test("a 409 that settles the action also invalidates the bell (#800)", async () => {
  // A 409 IS a resolution: the server has terminally settled the action and already retired its
  // bell row. The api client only announces on the fulfilled path, so without an explicit signal
  // here the badge keeps counting an alert the server has dropped, for up to a 60s poll.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
    delivering_verbs: ["continue", "choose", "answer"],
  });
  vi.mocked(api.approveAction).mockRejectedValue(
    new ApiError(409, "the session moved on", action({ state: "stale" })),
  );
  const seen = vi.fn();
  window.addEventListener(ACTION_RESOLVED_EVENT, seen);
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /^approve$/i }),
  );

  await waitFor(() => expect(seen).toHaveBeenCalled());
  window.removeEventListener(ACTION_RESOLVED_EVENT, seen);
});

test("a 409 carrying only a detail is not a resolution and must not invalidate", async () => {
  // The counterpart guard: `{detail}` with no settled record means the action is still live.
  // Firing the event there would tell the bell to drop an alert that still needs the operator.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [],
    expired_now: 0,
    delivering_verbs: ["continue", "choose", "answer"],
  });
  vi.mocked(api.approveAction).mockRejectedValue(
    new ApiError(409, "another caller claimed this action first"),
  );
  const seen = vi.fn();
  window.addEventListener(ACTION_RESOLVED_EVENT, seen);
  renderIt();
  await userEvent.click(
    await screen.findByRole("button", { name: /^approve$/i }),
  );

  await waitFor(() =>
    expect(screen.getByText(/not sent/i)).toBeInTheDocument(),
  );
  expect(seen).not.toHaveBeenCalled();
  window.removeEventListener(ACTION_RESOLVED_EVENT, seen);
});

// --- the server's projection is the authority on controls (#852 / #840 §16) -------------------

test("an approved action is reject-only, because approving again is a no-op", async () => {
  // The row used to derive controls from `state`, treating `proposed` and `approved` alike — so
  // an `approved` action rendered Approve, a tap the backend refuses. The server decides this
  // once in `project_for_operator`; the row consumes it.
  renderRow({
    state: "approved",
    verb: "continue",
    projection: "in_flight_revocable",
    can_approve: false,
    can_reject: true,
  });
  expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
  expect(
    await screen.findByRole("button", { name: /reject|dismiss/i }),
  ).toBeInTheDocument();
});

test("a claimed action offers no controls at all", async () => {
  // `claimed` is live rather than terminal: the bytes are already going out, so neither control
  // can be honoured and neither is offered.
  renderRow({
    state: "claimed",
    verb: "continue",
    projection: "in_flight_locked",
    can_approve: false,
    can_reject: false,
  });
  await screen.findByText(/continue/i);
  expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /reject|dismiss/i })).toBeNull();
});

test("a proposed action still offers both", async () => {
  renderRow({
    state: "proposed",
    verb: "continue",
    projection: "actionable",
    can_approve: true,
    can_reject: true,
  });
  expect(
    await screen.findByRole("button", { name: /^approve$/i }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /reject|dismiss/i }),
  ).toBeInTheDocument();
});

test("a response without the projection falls back to the old behaviour, not to a guess", async () => {
  // A payload written before these fields existed must still render. The fallback reproduces the
  // PREVIOUS rule verbatim rather than inventing the new one — a surface that invents a
  // projection is the thing this contract removes.
  renderRow({ state: "proposed", verb: "continue" });
  expect(
    await screen.findByRole("button", { name: /^approve$/i }),
  ).toBeInTheDocument();
});

// -----------------------------------------------------------------------------------------
// #852 / #840 §16 — the row renders the SERVER's projection, and never re-derives it.
//
// The defect this pins: an `escalated` action that kept a delivering verb. Two roads reach
// `escalated`, and the orchestrator's confidence path (`confidence < confidence_min` under
// yolo) leaves the verb alone — so `continue` survives, `delivering` is true, and the row
// offered Approve for an action `actuator.deliver` answers with 409. The comment in this file's
// subject said an escalated row "has no delivering verb by definition"; it does.
// -----------------------------------------------------------------------------------------

test("an escalated action with a delivering verb offers Reject, never Approve", () => {
  renderRow({
    state: "escalated",
    verb: "continue",
    can_approve: false,
    can_reject: true,
  });
  expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  // The reject control is named by its aria-label, which says "dismiss" for an escalation.
  expect(
    screen.getByRole("button", { name: /dismiss this escalation/i }),
  ).toBeInTheDocument();
});

test("the row obeys can_approve even when the state would suggest otherwise", () => {
  // `proposed` is the one approvable state, so this is the strongest form of the assertion:
  // if the row were still deriving from `state`, it would render Approve here.
  renderRow({
    state: "proposed",
    verb: "continue",
    can_approve: false,
    can_reject: true,
  });
  expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
});

test("an unknown projection still lets the operator reject", () => {
  // The ledger could not be read. The row must not go silent — see `project_for_operator`.
  renderRow({
    state: undefined,
    verb: "continue",
    can_approve: false,
    can_reject: true,
  });
  expect(
    screen.getByRole("button", { name: /reject this action/i }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
});

// -----------------------------------------------------------------------------------------
// #877 — the two roads into an escalation, in the row the operator actually sees.
//
// Before this, `escalated` meant both "the model asked you a question" and "the model wanted to
// act and was not sure". The second kept a real delivering verb, so the operator was shown a
// runnable `continue`, asked to look at it, and had no way to say yes. The row is where that
// asymmetry has to become visible.
// -----------------------------------------------------------------------------------------

test("a low-confidence escalation offers Approve AND Reject", () => {
  renderRow({
    state: "escalated_low_confidence",
    verb: "continue",
    can_approve: true,
    can_reject: true,
  });
  expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
  // …and the reject control is a REJECT, not a dismiss: it stands beside a real Approve, so
  // "dismiss" would understate what declining it does.
  expect(
    screen.getByRole("button", { name: /reject this action/i }),
  ).toBeInTheDocument();
});

test("the visible DISMISS word appears only when there is nothing to approve", () => {
  // The word exists because beside the evidence disclosure a bare ✕ reads as "close that panel"
  // rather than "settle this". Wherever Approve stands next to it, the pairing already says what
  // the ✕ does — which is now true of a low-confidence escalation too.
  const { unmount } = renderRow({
    state: "escalated",
    verb: "escalate",
    can_approve: false,
    can_reject: true,
  });
  expect(screen.getByText("Dismiss")).toBeInTheDocument();
  unmount();

  renderRow({
    state: "escalated_low_confidence",
    verb: "continue",
    can_approve: true,
    can_reject: true,
  });
  expect(screen.queryByText("Dismiss")).toBeNull();
});

test("the row still obeys can_approve for the new state, never the state name", () => {
  // The strongest form: the state that CAN be approved, with the server saying no. If the row
  // were deriving controls from the state name — the failure this whole contract exists to
  // prevent — it would render Approve here and the tap would 409.
  renderRow({
    state: "escalated_low_confidence",
    verb: "continue",
    can_approve: false,
    can_reject: true,
  });
  expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  expect(screen.getByText("Dismiss")).toBeInTheDocument();
});

test("both kinds of escalation still explain themselves", () => {
  renderRow({
    state: "escalated_low_confidence",
    verb: "continue",
    escalation_reason: "confidence",
    can_approve: true,
    can_reject: true,
  });
  expect(screen.getByText(/below threshold/i)).toBeInTheDocument();
});

test("a model escalation says its number is confidence that the call is YOURS (#1060)", () => {
  // `DEFAULT_ORCH_PROMPT` defines confidence as "right AND safe", and for an escalation the right
  // action is to ask — so 0.90 means "0.90 sure this is your decision". Printed as
  // "conf 0.90 · needs your call" it read as confidence in an answer, the opposite claim.
  renderRow({
    state: "escalated",
    verb: "escalate",
    confidence: 0.9,
    escalation_reason: "model",
    can_approve: false,
    can_reject: true,
  });
  const chip = screen.getByText("needs your call · 0.90 sure");
  expect(chip).toHaveAttribute(
    "aria-label",
    "Needs your call — the supervisor is 0.90 sure this decision is yours",
  );
  expect(screen.queryByText(/^conf 0\.90/)).toBeNull();
});

test("where the number IS about a proposed action, the chip keeps its old shape", () => {
  // A low-confidence escalation of a real delivering verb: 0.40 really is confidence in the
  // proposed `continue`, so "conf 0.40 · below threshold" is the true reading.
  renderRow({
    state: "escalated_low_confidence",
    verb: "continue",
    confidence: 0.4,
    escalation_reason: "confidence",
    can_approve: true,
    can_reject: true,
  });
  expect(screen.getByText(/^conf 0\.40 · below threshold$/)).toBeInTheDocument();
  expect(screen.queryByText(/sure$/)).toBeNull();
});

test("a low-confidence escalation carries the same NEEDS-A-DECISION tone as any other", () => {
  // Status colour is load-bearing here (docs/design.md): amber `degraded` means "needs a
  // decision". A new escalation state that fell through `toneOf` would render as idle grey —
  // the row would still work, and would stop *looking* like something waiting on the operator,
  // which is the quietest of the seven ways a missed comparison could fail.
  const tone = (state: string) => {
    const { container, unmount } = renderRow({
      state: state as OrchestratorAction["state"],
      verb: "continue",
      can_approve: true,
      can_reject: true,
    });
    const cls = container.firstElementChild?.className ?? "";
    unmount();
    return cls;
  };
  const degraded = tone("proposed");
  expect(tone("escalated")).toBe(degraded);
  expect(tone("escalated_low_confidence")).toBe(degraded);
  // …and it is a real distinction, not every state returning the same string.
  expect(tone("delivered")).not.toBe(degraded);
});
