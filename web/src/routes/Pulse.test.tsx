/** The Pulse route, now MISSION CONTROL (#878).
 *
 * **This file is a MIGRATION, not a rewrite.** Every case below descends from one that tested
 * the card grid, the banner or the Ask box, and each keeps what it was actually guarding rather
 * than what it happened to assert. Where a behaviour genuinely no longer exists it is dropped
 * with the reason stated where it used to be — a deleted test with no explanation is
 * indistinguishable from a lost regression, and several of these guard real past ones (#754's
 * refresh-failure case, #803's chip collapse, #795's outcome wording).
 *
 * #948 P3 changed the front door: entering the section selects NOTHING and shows the new-mission
 * page; a mission is opened from the rail. The "Sessions without a mission" view and its filters
 * are gone. A decision for a session no mission holds briefly rendered in that session's own pane
 * (#948 P3); #1049 removed that strip, so such a decision now has no Approve surface at all — the
 * bell lists it without counting it (#1057), and the operator reads and types in the session.
 * `ActionRow`'s own behaviour is covered in `components/pulse/Orchestrator.test.tsx` and is NOT
 * re-asserted here — this file checks that the console renders it in the right place.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { SectionStateContext } from "../app/sectionState";
import { DashboardRetentionProvider } from "../app/DashboardRetentionContext";
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

/** The rail NAVIGATES — it lists missions. With no shell slot in a unit test it renders in place. */
const rail = () => screen.getByRole("navigation", { name: /missions/i });

/** Open a mission the way the operator does since #948 P3: nothing is selected on arrival, so a
 *  test that wants a mission's body clicks its rail row. */
async function openFromRail(title: string) {
  await userEvent.click(await within(rail()).findByText(title));
}

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
// REMOVED WITH THE UNTRACKED VIEW (#948 P3) — eleven cases that lived here, and where their
// guarantees went:
//
// * "a live session with no mission lists under UNTRACKED, with its line", "shows the per-session
//   synthesis line", "with no synthesis, the review's summary is still the session's line", "a
//   BLANK summary falls through to the intervention reason", "what the orchestrator last did
//   rides the row", "a live action replaces the history line": all six asserted the rows of the
//   "Sessions without a mission" view, which no longer exists anywhere under /mission. The
//   outcome wording (#795) is still pinned on `actionOutcome` in `lib/orchestratorAction.test.ts`.
// * "a decision on an untracked session is never dropped (#840)": the guarantee MOVED — such a
//   decision has no operator surface at all since #1049 removed the session pane's decision strip:
//   opening the pane is what invalidated the strip's own Approve. A MISSION-held decision still
//   renders in its mission's thread, which is what `e2e/pulse-unified.spec.ts` now pins.
// * the four #803 filter-chip cases ("unadopted cwds collapse into one Default chip", "selecting
//   Default narrows UNTRACKED", "two entities sharing a name still get two chips", "a folder ref
//   with no usable id still routes to Default"): they drove the untracked project select, which is
//   removed. The sessions sidebar's own Default chip is pinned in
//   `components/sidebar/Filters.test.tsx`.
// =============================================================================================

// =============================================================================================
// The console's own surfaces.
// =============================================================================================

test("no missions renders the new-mission page, not a blank (#878, #948)", async () => {
  renderPulse();
  // The invitation is the front door itself now: the new-mission page, with nothing selected and
  // therefore no mission header above it.
  expect(await screen.findByTestId("mission-landing")).toHaveTextContent(
    /what should this mission achieve\?/i,
  );
  expect(screen.queryByTestId("console-title")).not.toBeInTheDocument();
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
  // Nothing is selected on arrival (#948), so the decision is not on the front door…
  await screen.findByTestId("mission-landing");
  expect(screen.queryByText("Tests were not run.")).not.toBeInTheDocument();
  // …and it is in the mission's thread once the mission is opened.
  await openFromRail("Kimi transcript adapter");
  expect(await screen.findByText("Tests were not run.")).toBeInTheDocument();
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
  await openFromRail("Kimi transcript adapter");
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

  await openFromRail("Mission A");
  await waitFor(() =>
    expect(screen.getByTestId("console-title")).toHaveTextContent("Mission A"),
  );
  await userEvent.type(
    screen.getByTestId("composer-input"),
    "half-written thought",
  );
  expect(screen.getByTestId("composer-input")).toHaveValue(
    "half-written thought",
  );

  await userEvent.click(within(rail()).getByText("Mission B"));
  await waitFor(() =>
    expect(screen.getByTestId("composer-input")).toHaveValue(""),
  );
});

test("a filter that excludes the selected mission does NOT withdraw its decision (#879)", async () => {
  // A mission's decisions must not depend on what the rail is filtered to. The console originally
  // derived them from the same filtered array the rail used, so a filter that excluded the held
  // session removed its Approve/Reject row while the mission stayed selected and still said
  // `needs_you`. A decision you cannot see is a decision you cannot make.
  //
  // The session chips that first exposed this went with the untracked view (#948 P3); the filter
  // that remains is the MISSION filter, and it can exclude the selected mission outright — which
  // is the same hazard in its current form.
  const held = mission({
    id: "msn_a",
    title: "Mission A",
    project_id: "alpha",
    sessions: [{ session_key: "claude:c1", removed_at: null }],
  });
  const row = listRow({
    id: "msn_a",
    title: "Mission A",
    project_id: "alpha",
    session_keys: ["claude:c1"],
  });
  vi.mocked(api.missions).mockImplementation(async (opts) => {
    const filtered = Boolean(opts?.q);
    return {
      missions: filtered ? [] : [row],
      total: filtered ? 0 : 1,
      limit: 50,
      offset: 0,
      facets: { projects: [], states: [] },
      store_error: null,
    };
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
          title: "Held session",
          state: "needs_you",
          mission_id: "msn_a",
          project: { kind: "project", id: "alpha", name: "alpha" },
          pending_action: pact({ rationale: "Tests were not run." }),
        }),
      ],
    }),
  );
  renderPulse({ configured: true } as PulseConfig);

  await openFromRail("Mission A");
  expect(await screen.findByText("Tests were not run.")).toBeInTheDocument();

  // Search for something the mission does not match: the rail empties under the selection.
  await userEvent.type(
    screen.getByRole("searchbox", { name: "Search missions" }),
    "zzz",
  );
  await waitFor(() =>
    expect(api.missions).toHaveBeenCalledWith(
      expect.objectContaining({ q: "zzz" }),
    ),
  );
  await waitFor(() =>
    expect(within(rail()).queryByText("Mission A")).not.toBeInTheDocument(),
  );

  // The mission is still selected, so its decision is still there to be made.
  expect(screen.getByTestId("console-title")).toHaveTextContent("Mission A");
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
  await openFromRail("Kimi transcript adapter");

  // Scoped to the details: the thread renders the same events, which is correct and only
  // ambiguous here. The one details disclosure (#948) replaces the old Details tab.
  await userEvent.click(await screen.findByTestId("details-toggle"));
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
  // The route's heading — by role, since the new-mission page's own copy also says "Mission
  // control" (#948).
  await screen.findByRole("heading", { level: 1, name: /MISSION CONTROL/i });
  expect(screen.queryByRole("button", { name: /scan now/i })).toBeNull();
  for (const d of ["FAST", "MED", "SLOW"]) {
    expect(screen.queryByRole("button", { name: d })).toBeNull();
  }
  expect(screen.queryByText(/not scanned yet/i)).toBeNull();
  // (The "N live sessions" count that replaced the scan window, `console-counts`, went with the
  // untracked view in #948 P3 — there is no session list on this route left for it to count.)
});

// =============================================================================================
// #1233 — the rail paints its last read on return, and always revalidates.
//
// The shell's retention provider stays mounted; the route under it mounts and unmounts, which is
// what leaving Missions and coming back does. `roots` stands in for the server's hard scope.
// =============================================================================================

function RetainedPulse({
  show,
  roots,
  memory,
}: {
  show: boolean;
  roots: string[];
  /** The shell's section memory — where the rail's scope and filters live across a visit. */
  memory?: Map<string, unknown>;
}) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    project_roots: roots,
  } as unknown as AppConfig;
  return (
    <MemoryRouter>
      <SectionStateContext.Provider value={memory ?? null}>
        <ConfigCtx.Provider value={config}>
          <DashboardRetentionProvider>{show ? <Pulse /> : null}</DashboardRetentionProvider>
        </ConfigCtx.Provider>
      </SectionStateContext.Provider>
    </MemoryRouter>
  );
}

const railList = (title: string) => ({
  missions: [listRow({ title })],
  total: 1,
  limit: 50,
  offset: 0,
  facets: { projects: [], states: [] },
  store_error: null,
  snapshot: "snap",
});

/** Visit once so a read is retained, then leave. */
async function visitAndLeave(roots = ["/r"]) {
  vi.mocked(api.missions).mockResolvedValue(railList("Retained mission"));
  const view = render(<RetainedPulse show roots={roots} />);
  expect(await within(rail()).findByText("Retained mission")).toBeInTheDocument();
  view.rerender(<RetainedPulse show={false} roots={roots} />);
  return view;
}

test("a return to Missions paints the last read at once and still asks the server (#1233)", async () => {
  const view = await visitAndLeave();
  // The revalidation is held open: whatever is on the rail now came from the retained read.
  vi.mocked(api.missions).mockReset().mockReturnValue(new Promise(() => {}));
  view.rerender(<RetainedPulse show roots={["/r"]} />);
  expect(within(rail()).getByText("Retained mission")).toBeInTheDocument();
  // Retention decides what is drawn first, never whether to fetch.
  await waitFor(() => expect(api.missions).toHaveBeenCalledTimes(1));
});

test("the revalidation replaces the retained rows (#1233)", async () => {
  const view = await visitAndLeave();
  vi.mocked(api.missions).mockReset().mockResolvedValue(railList("Fresh mission"));
  view.rerender(<RetainedPulse show roots={["/r"]} />);
  expect(await within(rail()).findByText("Fresh mission")).toBeInTheDocument();
  expect(within(rail()).queryByText("Retained mission")).toBeNull();
});

test("a failed revalidation keeps the retained rows on the rail (#1233)", async () => {
  const view = await visitAndLeave();
  vi.mocked(api.missions).mockReset().mockRejectedValue(new Error("down"));
  view.rerender(<RetainedPulse show roots={["/r"]} />);
  await waitFor(() => expect(api.missions).toHaveBeenCalledTimes(1));
  expect(within(rail()).getByText("Retained mission")).toBeInTheDocument();
});

test("a read retained under another hard scope is never painted (#1233)", async () => {
  const view = await visitAndLeave(["/r"]);
  vi.mocked(api.missions).mockReset().mockReturnValue(new Promise(() => {}));
  view.rerender(<RetainedPulse show={false} roots={["/other"]} />);
  view.rerender(<RetainedPulse show roots={["/other"]} />);
  await waitFor(() => expect(api.missions).toHaveBeenCalledTimes(1));
  expect(within(rail()).queryByText("Retained mission")).toBeNull();
});

test("a read retained for another scope or filter set is never painted (#1233)", async () => {
  // The store fences the HARD scope; which rail a read answers for — Active vs Archived, and the
  // filters — is the rail's own key. The active rail is retained, then the operator's section
  // memory says the next visit opens on Archived.
  const memory = new Map<string, unknown>();
  vi.mocked(api.missions).mockResolvedValue(railList("Retained mission"));
  const view = render(<RetainedPulse show roots={["/r"]} memory={memory} />);
  expect(await within(rail()).findByText("Retained mission")).toBeInTheDocument();
  view.rerender(<RetainedPulse show={false} roots={["/r"]} memory={memory} />);

  memory.set("missions.archived", true);
  vi.mocked(api.missions).mockReset().mockReturnValue(new Promise(() => {}));
  view.rerender(<RetainedPulse show roots={["/r"]} memory={memory} />);
  await waitFor(() => expect(api.missions).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.missions).mock.calls[0][0]).toMatchObject({ archived: true });
  expect(within(rail()).queryByText("Retained mission")).toBeNull();
});
