/** The Pulse route, now MISSION CONTROL (#878).
 *
 * **This file is a MIGRATION, not a rewrite.** Every case below descends from one that tested
 * the card grid, the banner or the Ask box, and each keeps what it was actually guarding rather
 * than what it happened to assert. Where a behaviour genuinely no longer exists it is dropped
 * with the reason stated on the case that replaced it — a deleted test with no explanation is
 * indistinguishable from a lost regression, and several of these guard real past ones (#754's
 * refresh-failure case, #803's chip collapse, #795's outcome wording).
 *
 * The 27 originals map to three destinations:
 *   - the HEADER (scan depth, Scan now, the degraded-scan notice) — unchanged, tests kept as-is;
 *   - the RAIL's UNTRACKED group, which is where a session that used to be a card now lives;
 *   - `ActionRow`, whose own behaviour is covered in `components/pulse/Orchestrator.test.tsx`
 *     and is NOT re-asserted here — this file checks that the console renders it in the right
 *     place, not how it behaves once rendered.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type {
  AppConfig,
  Mission,
  MissionListRow,
  OrchestratorAction,
  PulseCard,
  PulseConfig,
  PulseOverview,
  PulseState,
} from "../types/api";
import Pulse from "./Pulse";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      projectEntities: vi.fn().mockResolvedValue({ projects: [] }),
      pulse: vi.fn(),
      pulseScan: vi.fn(),
      pulseAsk: vi.fn(),
      setPrefs: vi.fn(),
      orchestrator: vi.fn(),
      orchestrate: vi.fn(),
      evidence: vi.fn(),
      // The console's own reads (#862 routes).
      missions: vi.fn(),
      mission: vi.fn(),
      missionObjectives: vi.fn(),
      missionContext: vi.fn(),
      adoptMissionSession: vi.fn(),
    },
  };
});

function card(
  over: Partial<PulseCard> & { id: string; state: PulseState },
): PulseCard {
  return {
    engine: "claude",
    title: "A session",
    cwd: "/seed/alpha",
    project: { kind: "folder", id: "/seed/alpha", name: "alpha" },
    last_activity: Math.floor(Date.now() / 1000) - 100,
    ai_summary: "did a thing",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: null,
    // Untracked-ness is about being LIVE and unheld: a card that is not live never reaches the
    // rail at all, so every fixture here that expects a row must set it.
    live: true,
    synthesis: null,
    ...over,
  };
}

function pact(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "a1",
    state: "escalated",
    ts: Math.floor(Date.now() / 1000),
    tier: "yolo",
    session_id: "claude:c1",
    engine: "claude",
    title: "Docs pass",
    project: "infra",
    project_id: "p1",
    verb: "escalate",
    confidence: 0.9,
    rationale: "Blocked on a choice only you can make.",
    evidence: "none",
    ...over,
  } as OrchestratorAction;
}

function overview(over: Partial<PulseOverview> = {}): PulseOverview {
  return {
    cache_version: 1,
    generated_at: Math.floor(Date.now() / 1000) - 60,
    window_days: 3,
    scan_depth: "fast",
    input_fingerprint: "fp",
    synthesis_skipped: false,
    cards: [],
    ...over,
  };
}

function mission(over: Partial<Mission> = {}): Mission {
  return {
    id: "msn_1",
    title: "Kimi transcript adapter",
    instruction: null,
    brief: null,
    project_id: "agent-sessions",
    cwd: "/repo",
    engine: null,
    engine_source: null,
    state: "running",
    playbook_id: null,
    created_at: 1,
    updated_at: 1,
    closed_at: null,
    archived_at: null,
    archiving_at: null,
    unarchiving_at: null,
    outcome: null,
    sessions: [],
    ...over,
  };
}

/** A LIST row — `session_keys`, never `sessions`. Separate from `mission()` on purpose: the
 *  production list producer does not attach a roster, and a fixture that supplies one is exactly
 *  what hid the crash (#879 review). */
function listRow(over: Partial<MissionListRow> = {}): MissionListRow {
  return {
    id: "msn_1",
    title: "Kimi transcript adapter",
    project_id: "agent-sessions",
    cwd: "/repo",
    state: "running",
    created_at: 1,
    updated_at: 1,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [],
    ...over,
  };
}

function renderPulse(pulse: PulseConfig | undefined = undefined) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    pulse,
  } as AppConfig;
  return render(
    <MemoryRouter>
      <ConfigCtx.Provider value={config}>
        <Pulse />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
}

/** The rail NAVIGATES — missions plus one "Untracked · N" entry. It does not list sessions. */
const rail = () => screen.getByRole("navigation", { name: /missions/i });
/** The centre pane CARRIES CONTENT — the thread, the objectives stop, or the untracked
 *  sessions. Session assertions belong here; that split is why they are not ambiguous. */
const pane = () => screen.getByTestId("pane");

beforeEach(() => {
  vi.mocked(api.pulse).mockReset().mockResolvedValue(overview());
  vi.mocked(api.pulseScan).mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
  vi.mocked(api.orchestrator).mockReset().mockRejectedValue(new Error("off"));
  vi.mocked(api.missions)
    .mockReset()
    .mockResolvedValue({
      missions: [],
      total: 0,
      limit: 50,
      offset: 0,
      facets: { projects: [], states: [] },
      store_error: null,
    });
  vi.mocked(api.mission)
    .mockReset()
    .mockResolvedValue(mission({ events: [], events_next_seq: null }));
  vi.mocked(api.missionObjectives)
    .mockReset()
    .mockResolvedValue({ objectives: [] });
  vi.mocked(api.missionContext).mockReset().mockResolvedValue({
    id: "msn_1",
    project_id: "agent-sessions",
    cwd: "/repo",
    sessions: [],
    git: null,
    git_error: null,
  });
});

// =============================================================================================
// The header — unchanged by this phase. These three are kept VERBATIM; the scan controls and
// the degraded-scan notice were never part of the grid.
// =============================================================================================

// =============================================================================================
// UNTRACKED — where a session that used to be a card now lives.
//
// The card carried four things: a title + jump link, a summary line, its pending decision, and
// what the orchestrator last did. All four survive; only their host changed. Losing any of them
// would have made the rail say LESS about a session than the grid did, which is not a trade the
// issue asked for.
// =============================================================================================

test("a live session with no mission lists under UNTRACKED, with its line (#441 P5, #754)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "codex:c-need",
          engine: "codex",
          title: "Deploy step",
          state: "needs_you",
        }),
        card({
          id: "claude:c-live",
          title: "Failing build",
          state: "in_flight",
        }),
      ],
    }),
  );
  renderPulse();
  const rows = await screen.findAllByTestId("untracked-session");
  expect(rows).toHaveLength(2);
  expect(within(pane()).getByText("Deploy step")).toBeInTheDocument();
  expect(within(pane()).getByText("Failing build")).toBeInTheDocument();
  // The card's summary line, kept.
  expect(rows[0]).toHaveTextContent("did a thing");
  // …and the band still rides the LED's accessible name, since colour alone is not a state.
  expect(within(rows[0]).getByRole("img")).toHaveAccessibleName("Needs you");
});

test("shows the per-session synthesis line instead of the summary when present (#441 P4/P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "idle",
          ai_summary: "old summary",
          synthesis: "waiting on your review of the parser",
        }),
      ],
    }),
  );
  renderPulse();
  expect(
    await within(await screen.findByTestId("pane")).findByText(
      "waiting on your review of the parser",
    ),
  ).toBeInTheDocument();
  expect(screen.queryByText("old summary")).not.toBeInTheDocument();
});

test("with no synthesis, the review's summary is still the session's line (#781)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({ id: "claude:c1", state: "idle", ai_summary: "reviewed it" }),
      ],
    }),
  );
  renderPulse();
  expect(await screen.findByText("reviewed it")).toBeInTheDocument();
});

test("a BLANK summary falls through to the intervention reason — a row never says nothing (#781)", async () => {
  // The original asserted this against the card body. The fallback chain is the regression, not
  // the element it rendered into: an empty `ai_summary` is a real persisted shape.
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "needs_you",
          ai_summary: "",
          synthesis: null,
          intervention_required: true,
          intervention_reason: "Confirm the push",
        }),
      ],
    }),
  );
  renderPulse();
  expect(await screen.findByText("Confirm the push")).toBeInTheDocument();
});

test("what the orchestrator last did rides the row, worded as an OUTCOME (#777, #795)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "idle",
          last_action: pact({ state: "expired", verb: "escalate", repeats: 7 }),
        }),
      ],
    }),
  );
  renderPulse();
  const line = await screen.findByTestId("untracked-view-last-action");
  expect(line).toHaveTextContent("ESCALATE");
  // #795: what BECAME of it, never the ledger state — `expired` names a transition in a state
  // machine the operator never sees.
  expect(line).toHaveTextContent("no decision in time");
  expect(line).not.toHaveTextContent("expired");
  expect(line).toHaveTextContent("×7");
});

test("a live action replaces the history line, and renders its controls (#777)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "needs_you",
          pending_action: pact({ state: "proposed", verb: "continue" }),
          last_action: pact({
            id: "old",
            state: "delivered",
            verb: "continue",
          }),
        }),
      ],
    }),
  );
  renderPulse();
  // The block shows no history line while something is pending…
  await screen.findByTestId("untracked-session");
  expect(
    screen.queryByTestId("untracked-view-last-action"),
  ).not.toBeInTheDocument();
  // …and the decision itself renders, with real controls.
  expect(
    await screen.findByRole("button", { name: /approve/i }),
  ).toBeInTheDocument();
});

test("a decision on an untracked session is never dropped (#840)", async () => {
  // The regression this file exists to prevent. Before the console, this decision rode a card;
  // if UNTRACKED had no view of its own it would have had nowhere to render, and a console that
  // silently loses decisions for unorganised work is worse than the grid it replaced.
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "needs_you",
          pending_action: pact({
            rationale: "Blocked on a choice only you can make.",
          }),
        }),
      ],
    }),
  );
  renderPulse();
  expect(await screen.findByTestId("rail-untracked-view")).toHaveTextContent(
    "1 waiting on you",
  );
  expect(
    within(pane()).getByText("Blocked on a choice only you can make."),
  ).toBeInTheDocument();
});

// =============================================================================================
// The filter chips (#803). They were never the grid's — they belong to the session list, and the
// session list moved. All four originals are kept, retargeted at the rows they now narrow.
// =============================================================================================

test("unadopted cwds collapse into one Default chip, never a raw path (#803)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:a",
          state: "idle",
          cwd: "/tmp/scratch-one",
          project: { kind: "folder", id: "", name: "" },
        }),
        card({
          id: "claude:b",
          state: "idle",
          cwd: "/tmp/scratch-two",
          project: { kind: "folder", id: "", name: "" },
        }),
        card({
          id: "claude:c",
          state: "idle",
          project: { kind: "project", id: "p1", name: "battlelab" },
        }),
      ],
    }),
  );
  renderPulse();
  expect(
    await screen.findByRole("option", { name: /default/i }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/tmp\/scratch-one/)).not.toBeInTheDocument();
  expect(screen.queryByText(/tmp\/scratch-two/)).not.toBeInTheDocument();
});

test("selecting Default narrows UNTRACKED to exactly the unadopted sessions (#803)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:a",
          title: "Scratch one",
          state: "idle",
          cwd: "/tmp/scratch-one",
          project: { kind: "folder", id: "", name: "" },
        }),
        card({
          id: "claude:c",
          title: "Real project",
          state: "idle",
          project: { kind: "project", id: "p1", name: "battlelab" },
        }),
      ],
    }),
  );
  renderPulse();
  await userEvent.selectOptions(
    await screen.findByRole("combobox", {
      name: "Filter untracked sessions by project",
    }),
    await screen.findByRole("option", { name: /default/i }),
  );
  expect(within(pane()).getByText("Scratch one")).toBeInTheDocument();
  expect(within(pane()).queryByText("Real project")).not.toBeInTheDocument();
});

test("two entities sharing a name still get two chips (#754 regression, #803)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:a",
          state: "idle",
          cwd: "/one/api",
          project: { kind: "project", id: "p1", name: "api" },
        }),
        card({
          id: "claude:b",
          state: "idle",
          cwd: "/two/api",
          project: { kind: "project", id: "p2", name: "api" },
        }),
      ],
    }),
  );
  renderPulse();
  // Two chips, disambiguated — collapsing them by label showed one chip carrying both.
  const chips = await screen.findAllByRole("option", { name: /api/i });
  expect(chips.length).toBeGreaterThanOrEqual(2);
});

test("a folder ref with no usable id still routes to Default (#803)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:a",
          state: "idle",
          cwd: "/tmp/x",
          project: { kind: "folder", id: "", name: "" },
        }),
        card({
          id: "claude:b",
          state: "idle",
          project: { kind: "project", id: "p1", name: "battlelab" },
        }),
      ],
    }),
  );
  renderPulse();
  expect(
    await screen.findByRole("option", { name: /default/i }),
  ).toBeInTheDocument();
});

// =============================================================================================
// The console's own surfaces.
// =============================================================================================

test("no missions and no live sessions renders an invitation, not a blank (#878)", async () => {
  renderPulse();
  expect(await screen.findByTestId("console-empty")).toHaveTextContent(
    /nothing tracked yet/i,
  );
});

test("a store that will not answer says so, and is not 'you have no missions' (#878)", async () => {
  vi.mocked(api.missions).mockResolvedValue({
    missions: [],
    total: 0,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: "the mission store is locked",
  });
  renderPulse();
  // Announced in BOTH the rail and the pane, deliberately: on a phone the rail is a drawer, so
  // a rail-only notice is invisible exactly when the console is degraded.
  expect(await screen.findByTestId("console-store-error")).toBeInTheDocument();
  expect(
    within(rail()).getByText(/the mission store could not be read/i),
  ).toBeInTheDocument();
  // …and the invitation that WOULD claim there is simply nothing is withheld.
  expect(screen.queryByTestId("rail-no-missions")).not.toBeInTheDocument();
});

test("a mission's own decision renders in its thread (#840 §15)", async () => {
  const held = mission({
    sessions: [{ session_key: "claude:c1", removed_at: null }],
  });
  vi.mocked(api.missions).mockResolvedValue({
    missions: [listRow({ session_keys: ["claude:c1"] })],
    total: 1,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: null,
  });
  vi.mocked(api.mission).mockResolvedValue({
    ...held,
    events: [],
    events_next_seq: null,
  });
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "needs_you",
          // OWNERSHIP RIDES THE CARD, stamped by the server over every mission — loaded or not.
          // The console used to derive it from the mission rows it had in memory, and that rail
          // is paged, so a session held by an unloaded mission read as unheld and was offered an
          // adoption the server refuses. A fixture that omits this is no longer producer-faithful.
          mission_id: "msn_1",
          pending_action: pact({ rationale: "Tests were not run." }),
        }),
      ],
    }),
  );
  renderPulse();
  // It is in the mission's thread — and NOT in UNTRACKED, because the mission holds it.
  expect(await screen.findByText("Tests were not run.")).toBeInTheDocument();
  expect(screen.queryByTestId("untracked-session")).not.toBeInTheDocument();
});

test("with no AI endpoint the composer is disabled and the notice says what is off (#878)", async () => {
  vi.mocked(api.missions).mockResolvedValue({
    missions: [listRow()],
    total: 1,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: null,
  });
  renderPulse();
  expect(await screen.findByTestId("no-ai-notice")).toHaveTextContent(
    /and the composer/i,
  );
  expect(screen.getByTestId("composer-input")).toBeDisabled();
  expect(screen.getByTestId("composer-send")).toBeDisabled();
  // The original Ask test asserted "makes no call". Still true, and still the point.
  expect(api.pulseAsk).not.toHaveBeenCalled();
});

test("switching missions starts a clean composer — the console KEYS the mission body (#878)", async () => {
  // The console's half of the composer's ownership guarantee, and the half its own test file
  // cannot see: `Composer.test.tsx` supplies the key itself, so it stays green even if the
  // console stops keying. This detects exactly that — an unkeyed body keeps one instance across
  // the switch, so the draft (and the busy flag) would follow the operator into a mission they
  // were never typed for.
  const a = mission({ id: "msn_a", title: "Mission A" });
  const b = mission({ id: "msn_b", title: "Mission B" });
  vi.mocked(api.missions).mockResolvedValue({
    missions: [
      listRow({ id: "msn_a", title: "Mission A" }),
      listRow({ id: "msn_b", title: "Mission B" }),
    ],
    total: 2,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: null,
  });
  vi.mocked(api.mission).mockImplementation(async (id: string) => ({
    ...(id === "msn_a" ? a : b),
    events: [],
    events_next_seq: null,
  }));
  renderPulse({ configured: true } as PulseConfig);

  // TWO matches now, and both are correct: the rail row names the mission and so does the
  // console header (#942 — the header reads its title from the mission the body is rendering,
  // which is what removed the "Select a mission" contradiction). This test is about the
  // composer being keyed per mission, so it only needs to wait until the mission is on screen.
  await screen.findAllByText("Mission A");
  await userEvent.type(
    screen.getByTestId("composer-input"),
    "half-written thought",
  );
  expect(screen.getByTestId("composer-input")).toHaveValue(
    "half-written thought",
  );

  await userEvent.click(screen.getByText("Mission B"));
  await waitFor(() =>
    expect(screen.getByTestId("composer-input")).toHaveValue(""),
  );
});

test("a filter that excludes a held session does NOT withdraw its mission's decision (#879)", async () => {
  // The chips narrow the session LIST. They were never meant to withdraw a decision — but the
  // console originally derived a mission's decisions from the same filtered array the rail uses,
  // so choosing a project chip that excluded a held session removed its Approve/Reject row while
  // the mission stayed selected and still said `needs_you`. A decision you cannot see is a
  // decision you cannot make.
  const held = mission({
    id: "msn_a",
    title: "Mission A",
    project_id: "alpha",
    sessions: [{ session_key: "claude:c1", removed_at: null }],
  });
  vi.mocked(api.missions).mockResolvedValue({
    missions: [
      listRow({
        id: "msn_a",
        title: "Mission A",
        project_id: "alpha",
        session_keys: ["claude:c1"],
      }),
    ],
    total: 1,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: null,
  });
  vi.mocked(api.mission).mockResolvedValue({
    ...held,
    events: [],
    events_next_seq: null,
  });
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        // The mission's own session, in project "alpha"…
        card({
          id: "claude:c1",
          title: "Held session",
          state: "needs_you",
          project: { kind: "project", id: "alpha", name: "alpha" },
          pending_action: pact({ rationale: "Tests were not run." }),
        }),
        // …and an unrelated one in another project, so the chips render at all.
        card({
          id: "codex:c2",
          engine: "codex",
          title: "Other work",
          state: "idle",
          project: { kind: "project", id: "beta", name: "beta" },
        }),
      ],
    }),
  );
  renderPulse({ configured: true } as PulseConfig);

  expect(await screen.findByText("Tests were not run.")).toBeInTheDocument();

  // Filter to the OTHER project, which excludes the held session entirely.
  await userEvent.click(screen.getByTestId("rail-untracked-view"));
  await userEvent.selectOptions(
    await screen.findByRole("combobox", {
      name: "Filter untracked sessions by project",
    }),
    "beta",
  );
  await userEvent.click(screen.getByTestId("rail-mission"));

  // The mission is still selected, so its decision is still there to be made.
  expect(screen.getByText("Tests were not run.")).toBeInTheDocument();
});

test("mission 101 is reachable — the rail follows `total`, it does not cap (#879)", async () => {
  // The rail's contract is "every mission". The first version asked for 100 and ignored
  // `total`, so a 101st was unreachable with nothing on screen to say it existed.
  const page1 = Array.from({ length: 100 }, (_, i) =>
    listRow({ id: `msn_${i}`, title: `Mission ${i}` }),
  );
  const page2 = [listRow({ id: "msn_100", title: "Mission 100" })];
  vi.mocked(api.missions).mockImplementation(async (opts) =>
    (opts?.offset ?? 0) === 0
      ? {
          missions: page1,
          total: 101,
          limit: 100,
          offset: 0,
          facets: { projects: [], states: [] },
          store_error: null,
        }
      : {
          missions: page2,
          total: 101,
          limit: 100,
          offset: 100,
          facets: { projects: [], states: [] },
          store_error: null,
        },
  );
  vi.mocked(api.mission).mockResolvedValue(
    mission({ id: "msn_0", events: [], events_next_seq: null }),
  );
  renderPulse();

  const more = await screen.findByTestId("rail-load-more");
  expect(more).toHaveTextContent("100 of 101");
  expect(screen.queryByText("Mission 100")).not.toBeInTheDocument();

  await userEvent.click(more);
  expect(await screen.findByText("Mission 100")).toBeInTheDocument();
  // …and once the set is complete the continuation goes away rather than offering a no-op.
  await waitFor(() =>
    expect(screen.queryByTestId("rail-load-more")).not.toBeInTheDocument(),
  );
});

test("the timeline pages by CURSOR across more than one page (#878)", async () => {
  // Cursor, never offset: events arrive while the operator reads, so an offset window shifts
  // under them and a second page would duplicate or skip rows. Two real pages, and the second
  // request must carry the FIRST page's `events_next_seq`.
  const ev = (seq: number) => ({
    seq,
    mission_id: "msn_1",
    at: 1_700_000_000,
    kind: "note",
    session_key: null,
    action_id: null,
    text: `event ${seq}`,
    meta: null,
    settlement: null,
  });
  vi.mocked(api.missions).mockResolvedValue({
    missions: [listRow()],
    total: 1,
    limit: 50,
    offset: 0,
    facets: { projects: [], states: [] },
    store_error: null,
  });
  vi.mocked(api.mission).mockImplementation(async (_id, opts) =>
    opts?.before == null
      ? mission({ events: [ev(9), ev(8)], events_next_seq: 8 })
      : mission({ events: [ev(7)], events_next_seq: null }),
  );
  renderPulse();

  // Scoped to the pane: jsdom applies no media queries, so the persistent detail column renders
  // the same timeline alongside the tab strip. That duplication is correct in a browser at
  // ≥1400px and only ambiguous here.
  await userEvent.click(await screen.findByTestId("stop-details"));
  await userEvent.click(screen.getByTestId("detail-timeline"));
  const pane = await screen.findByTestId("mission-details");
  expect(await within(pane).findByText("event 9")).toBeInTheDocument();
  expect(within(pane).queryByText("event 7")).not.toBeInTheDocument();

  await userEvent.click(within(pane).getByTestId("timeline-more"));
  expect(await within(pane).findByText("event 7")).toBeInTheDocument();
  // The second request carried the cursor, not a page number.
  expect(api.mission).toHaveBeenLastCalledWith(
    "msn_1",
    expect.objectContaining({ before: 8 }),
  );
  // Exhausted: no continuation offered once the cursor comes back null.
  await waitFor(() =>
    expect(within(pane).queryByTestId("timeline-more")).not.toBeInTheDocument(),
  );
});

/** The route's scan chrome moved out (#929).
 *
 * Three tests lived here for the header's `Scan now` button and its FAST/MED/SLOW depth
 * selector: the 409 "already running" path, depth persistence, and the synthesis-skipped
 * notice. #929 removed that chrome from the ROUTE — it was Pulse's operator-triggered scan
 * model, left behind when MISSION CONTROL replaced the dashboard.
 *
 * They are NOT replaced by weaker assertions here, because the behaviour they covered did not
 * move to this page: `routes/PulseSettings.tsx` owns `scan_depth`, `window_days` and the manual
 * scan path, and `PulseSettings.test.tsx` is where that surface is pinned. Deleting a test whose
 * subject moved is right; quietly re-asserting a shadow of it here would be worse than nothing.
 *
 * What this file DOES still pin about the removal is below: the chrome is gone from the route.
 */

test("the route carries no scan chrome — that lives in Settings now (#929)", async () => {
  renderPulse();
  await screen.findByText(/MISSION CONTROL/i);
  expect(screen.queryByRole("button", { name: /scan now/i })).toBeNull();
  for (const d of ["FAST", "MED", "SLOW"]) {
    expect(screen.queryByRole("button", { name: d })).toBeNull();
  }
  // …and the header now orients on live sessions rather than on when a scan last ran.
  // (The project/agent chips are a MISSION CONTROL surface and stay, but they render only
  // when the overview has facets, so they are pinned in the filter tests rather than here.)
  expect(screen.queryByText(/not scanned yet/i)).toBeNull();
  expect(screen.getByTestId("console-counts")).toBeInTheDocument();
});
