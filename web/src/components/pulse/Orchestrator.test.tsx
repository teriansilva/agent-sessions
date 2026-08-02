/** Pulse orchestrator strip (#726 Phases 1–2).
 *
 * Pinned: the ceiling is SHOWN (the tier alone must never imply more than it grants); only a
 * DELIVERING verb offers approve/reject, so the UI never implies an escalation would be sent;
 * a 409 from compare-and-execute reads as "nothing was sent", distinguishable from an error;
 * evidence is fetched from the server on expand rather than rendered from anything the model
 * said; every feed row names its project; and a failing endpoint degrades to no strip, not a blank
 * page.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { ActionRow } from "./ActionRow";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { OrchestratorAction, OrchestratorConfig } from "../../types/api";
import { Orchestrator } from "./Orchestrator";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      orchestrator: vi.fn(),
      orchestrate: vi.fn(),
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
    prompt: "p",
    notify: "escalations",
    configured: true,
    default_prompt: "p",
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
      <Orchestrator />
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
  vi.mocked(api.orchestrate).mockReset();
  vi.mocked(api.evidence).mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
  vi.mocked(api.approveAction).mockReset();
  vi.mocked(api.rejectAction).mockReset();
});

test("shows the server-owned autonomy ceiling, not just the tier (#726)", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config({ autonomy: "yolo" }),
    pending: [],
    feed: [],
    expired_now: 0,
  });
  renderIt();
  // YOLO is selected — but the copy must still say what it can actually deliver on its own,
  // because the tier name alone reads as "does everything".
  expect(await screen.findByRole("button", { name: "YOLO" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByText(/acts on its own:/i)).toHaveTextContent("continue");
  expect(
    screen.getByText(/everything else always waits for you/i),
  ).toBeInTheDocument();
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
  expect(await screen.findByText(/2 actions need you/i)).toBeInTheDocument();
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

test("evidence is fetched from the server on expand, never rendered from the model", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [action()],
    expired_now: 0,
  });
  vi.mocked(api.evidence).mockResolvedValue({
    kind: "screen",
    text: "› waiting for input",
    available: true,
  });
  renderIt();
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

test("every feed row names its project, since the grid no longer groups by one", async () => {
  // The feed used to render one `<ul>` per project under a heading. In a grid that costs a
  // partial row per project — measured at 1900px a two-action project filled 2 of 4 tracks —
  // so it is one flat grid now and the row carries the project itself (#754). The information
  // has to survive the layout change; that is what this pins.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [
      action({ id: "a1", project: "agent-sessions" }),
      action({
        id: "a2",
        project: "battlelab-cloud",
        session_id: "codex:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      }),
      action({
        id: "a3",
        project: "agent-sessions",
        session_id: "kimi:session_cccccccc",
      }),
    ],
    expired_now: 0,
  });
  renderIt();
  // One label per ROW now, not one heading per group: two rows are agent-sessions.
  expect(await screen.findAllByText("agent-sessions")).toHaveLength(2);
  expect(screen.getAllByText("battlelab-cloud")).toHaveLength(1);
  // …and the group headers with their counts are gone.
  expect(screen.queryByText("2 actions")).toBeNull();
  expect(screen.queryByText("1 action")).toBeNull();
});

test("a failing endpoint degrades to no strip, never a blank Pulse page", async () => {
  vi.mocked(api.orchestrator).mockRejectedValue(new Error("down"));
  const { container } = renderIt();
  await waitFor(() => expect(container.querySelector("section")).toBeNull());
});

test("changing the tier persists it and refreshes the shared config", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [],
    expired_now: 0,
  });
  const onTierChange = vi.fn();
  render(
    <MemoryRouter>
      <Orchestrator onTierChange={onTierChange} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("button", { name: "OFF" }));
  expect(api.setPrefs).toHaveBeenCalledWith({
    orchestrator: { autonomy: "off" },
  });
  // Without the refresh the Settings panel would show pre-save values on remount (#667).
  await waitFor(() => expect(onTierChange).toHaveBeenCalled());
});

test("evidence is re-fetched on every open — 'live' must not mean 'cached once'", async () => {
  // The server serves evidence uncached so the operator always reads the CURRENT screen.
  // Caching the first snapshot client-side quietly defeats that, and shows a screen the
  // session has moved past — exactly what must not be approved against.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [action()],
    expired_now: 0,
  });
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
  renderIt();
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

// --- a dead endpoint must not look like a quiet day (#772) -----------------------------------

function withHealth(last: Record<string, unknown> | undefined) {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [],
    expired_now: 0,
    ...(last ? { last } : {}),
  } as never);
}

test("a run of failed passes says so, with how long it has been failing", async () => {
  // The 11-hour outage: every pass threw `ReviewError: endpoint returned HTTP 500`, and this
  // panel rendered exactly as it does on a quiet day. "Nothing needs you" and "nothing has
  // been looked at since yesterday evening" have to be distinguishable.
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000,
      ok: false,
      error: "ReviewError: endpoint returned HTTP 500",
      consecutive_failures: 4,
      last_ok: Date.now() / 1000 - 11 * 3600,
    },
  });
  renderIt();
  const line = await screen.findByRole("status");
  expect(line).toHaveTextContent(/can’t reach its AI endpoint/i);
  expect(line).toHaveTextContent(/HTTP 500/);
  expect(line).toHaveTextContent(/11 hours ago/i);
});

test("a single failure stays silent — one blip is not an outage", async () => {
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000,
      ok: false,
      error: "ReviewError: endpoint returned HTTP 502",
      consecutive_failures: 1,
      last_ok: Date.now() / 1000 - 600,
    },
  });
  renderIt();
  await screen.findByText(/autonomy/i);
  expect(screen.queryByRole("status")).toBeNull();
});

test("a healthy last run says nothing at all", async () => {
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000,
      ok: true,
      error: null,
      consecutive_failures: 0,
      last_ok: Date.now() / 1000,
    },
  });
  renderIt();
  await screen.findByText(/autonomy/i);
  expect(screen.queryByRole("status")).toBeNull();
});

test("an older server with no health record renders unchanged", async () => {
  // `last` is absent from a server that predates this — the panel must not decide that means
  // "failing" and paint a warning over a perfectly working install.
  withHealth(undefined);
  renderIt();
  await screen.findByText(/autonomy/i);
  expect(screen.queryByRole("status")).toBeNull();
});

test("the endpoint's message is TEXT, never markup", async () => {
  // It is a remote server's response body. React escapes it like any other string; this pins
  // that nobody later reaches for dangerouslySetInnerHTML to make it look nicer.
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000,
      ok: false,
      error: "ReviewError: <img src=x onerror=alert(1)>",
      consecutive_failures: 3,
      last_ok: Date.now() / 1000 - 3600,
    },
  });
  renderIt();
  const line = await screen.findByRole("status");
  expect(line.querySelector("img")).toBeNull();
  expect(line).toHaveTextContent(/<img src=x onerror=alert\(1\)>/);
});

test("a successful Run now clears the degraded line it was told to fix", async () => {
  // The regression Hermes caught. `health` was only ever set by the mount effect, so the pass
  // the operator ran to FIX the outage left the outage warning on screen until a remount —
  // and "Run now" is precisely what the guidance says to click. The response carries the
  // health record now, so the pass that ran is the evidence that clears it.
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000 - 300,
      ok: false,
      error: "ReviewError: endpoint returned HTTP 500",
      consecutive_failures: 5,
      last_ok: Date.now() / 1000 - 11 * 3600,
    },
  });
  vi.mocked(api.orchestrate).mockResolvedValue({
    assessment: "",
    config: config(),
    pending: [],
    feed: [],
    expired_now: 0,
    last: {
      orchestrator: {
        finished_at: Date.now() / 1000,
        ok: true,
        error: null,
        consecutive_failures: 0,
        last_ok: Date.now() / 1000,
      },
    },
  } as never);

  renderIt();
  expect(await screen.findByRole("status")).toHaveTextContent(/HTTP 500/);

  await userEvent.click(screen.getByRole("button", { name: /run now/i }));

  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
});

test("a Run now that also fails leaves the line, with the newer count", async () => {
  // The other half: a manual pass is health evidence whichever way it goes.
  withHealth({
    orchestrator: {
      finished_at: Date.now() / 1000 - 300,
      ok: false,
      error: "ReviewError: endpoint returned HTTP 500",
      consecutive_failures: 2,
      last_ok: Date.now() / 1000 - 3600,
    },
  });
  vi.mocked(api.orchestrate).mockRejectedValue(
    new ApiError(502, "endpoint failure"),
  );
  renderIt();
  expect(await screen.findByRole("status")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /run now/i }));
  // Still degraded — the mount record stands, because the failed call carried no newer one.
  await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
});
