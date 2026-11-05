import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo, StructuredSnapshot, StructuredTurn } from "../../types/api";
import type { PluginCatalog, PluginGeneration, PluginInstallation } from "../../types/plugins";
import { signinPath } from "../agents/agentStatus";
import { setupPath } from "../plugins/model";
import { StructuredPane } from "./StructuredPane";

/** A turn that failed on the agent's login (`remedy: "signin"`) offers Sign in to the console
 *  agent whose login the session uses, read from the roster's `api.source`. */

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      structuredSubmit: vi.fn(),
      templates: vi.fn(),
      templateVariables: vi.fn(() => Promise.resolve({ variables: [], limits: {} })),
      plugins: vi.fn(),
      session: vi.fn(() => new Promise(() => {})),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const ENGINE = fixture.engines.find((e) => e.runtime === "api" && e.api?.source)!;
const SOURCE = fixture.engines.find((e) => e.id === ENGINE.api!.source)!;
const KEY = `${ENGINE.id}:${ID}`;

function turn(over: Partial<StructuredTurn> = {}): StructuredTurn {
  const id = crypto.randomUUID();
  return {
    turn_id: id,
    operation_id: id,
    state: "failed",
    text: "hi",
    reply: "Failed to authenticate: OAuth session expired and could not be refreshed",
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    ...over,
  };
}

function snap(turns: StructuredTurn[], over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns,
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function catalog(row: Partial<PluginInstallation>): PluginCatalog {
  const gen = (id: string) => ({ id }) as unknown as PluginGeneration;
  return {
    plugins: [{ id: SOURCE.id, enabled: true, generations: [gen("g-active"), gen("g-new")], ...row }],
    catalog: [],
    operations: [],
    feed: { state: "ready", sequence: 1, error: null },
  } as unknown as PluginCatalog;
}

function renderPane() {
  return render(
    <MemoryRouter>
      <StructuredPane engine={ENGINE.id} id={ID} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.structuredEvents).mockResolvedValue({ session_key: KEY, revision: 4, next_cursor: 4, events: [] });
  vi.mocked(api.plugins).mockResolvedValue(catalog({ active: "g-active", candidate: "g-active" }));
});

afterEach(() => {
  vi.clearAllMocks();
  resetRoster();
});

test("a sign-in failure offers Sign in to the source console agent, by its label", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([turn({ remedy: "signin" })]));
  renderPane();
  const link = await screen.findByRole("link", { name: `Sign in to ${SOURCE.label}` });
  expect(screen.getByTestId("structured-failed")).toHaveTextContent("failed");
  await waitFor(() => expect(api.plugins).toHaveBeenCalled());
  expect(link).toHaveAttribute("href", setupPath({ plugin: SOURCE.id }));
});

test("a waiting candidate generation is where Sign in goes", async () => {
  vi.mocked(api.plugins).mockResolvedValue(catalog({ active: "g-active", candidate: "g-new" }));
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([turn({ remedy: "signin" })]));
  renderPane();
  const link = await screen.findByRole("link", { name: `Sign in to ${SOURCE.label}` });
  await waitFor(() =>
    expect(link).toHaveAttribute("href", setupPath({ plugin: SOURCE.id, generation: "g-new" })),
  );
});

test.each([
  ["a failure with no remedy", turn()],
  ["a completed turn", turn({ state: "completed", remedy: "signin" })],
  ["an interrupted turn", turn({ state: "interrupted", remedy: "signin" })],
])("%s offers no Sign in", async (_, t) => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([t]));
  renderPane();
  await screen.findByTestId("structured-reply");
  expect(screen.queryByTestId("structured-signin")).toBeNull();
  expect(api.plugins).not.toHaveBeenCalled();
});

test("a read-only view offers no Sign in", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap([turn({ remedy: "signin" })], { read_only: "another tab" }),
  );
  renderPane();
  await screen.findByTestId("structured-failed");
  expect(screen.queryByTestId("structured-signin")).toBeNull();
});

test("signinPath never sends sign-in to the active generation", () => {
  const id = SOURCE.id;
  expect(signinPath(id, null)).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, catalog({ active: "g-active", candidate: "g-active" }))).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, catalog({ active: "g-active", candidate: null }))).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, catalog({ active: "g-active", candidate: "g-gone" }))).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, catalog({ active: null, candidate: "g-new" }))).toBe(
    setupPath({ plugin: id, generation: "g-new" }),
  );
});

/** #1852: a leftover copy installed from an older catalog recipe is never the sign-in target. */
function withCandidate(review: Partial<PluginGeneration["review"]> | null, entry?: { digest: string; installable?: boolean }) {
  const data = catalog({ active: null, candidate: "g-old" });
  data.plugins[0].generations = [
    { id: "g-old", review: review && { plugin_id: SOURCE.id, source: "signed", recipe_digest: "old", ...review } } as unknown as PluginGeneration,
  ];
  data.catalog = entry ? [{ manifest: { identity: { id: SOURCE.id, label: SOURCE.label, publisher: "p", version: "2" } }, ...entry }] : [];
  return data;
}

test("signinPath skips a stale candidate and keeps a current, local or uncatalogued one", () => {
  const id = SOURCE.id;
  const atGeneration = setupPath({ plugin: id, generation: "g-old" });
  expect(signinPath(id, withCandidate({}, { digest: "new" }))).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, withCandidate({ source: "bundled" }, { digest: "new" }))).toBe(setupPath({ plugin: id }));
  expect(signinPath(id, withCandidate({}, { digest: "old" }))).toBe(atGeneration);
  expect(signinPath(id, withCandidate({ source: "local" }, { digest: "new" }))).toBe(atGeneration);
  expect(signinPath(id, withCandidate({}, { digest: "new", installable: false }))).toBe(atGeneration);
  expect(signinPath(id, withCandidate({}))).toBe(atGeneration);
  // A once-enabled copy is a kept version, not a leftover.
  const kept = withCandidate({}, { digest: "new" });
  kept.operations = [{ plugin_id: id, kind: "activate", state: "complete", generation_id: "g-old" } as PluginCatalog["operations"][number]];
  expect(signinPath(id, kept)).toBe(atGeneration);
});

test("a stale candidate's Sign in opens a fresh setup and says the new copy need not be enabled", async () => {
  vi.mocked(api.plugins).mockResolvedValue(withCandidate({}, { digest: "new" }));
  vi.mocked(api.structuredSnapshot).mockResolvedValue(snap([turn({ remedy: "signin" })]));
  renderPane();
  const note = await screen.findByTestId("structured-signin-via-setup");
  expect(note).toHaveTextContent("No current spare copy");
  expect(note).toHaveTextContent("you don’t need to enable the new copy");
  expect(screen.getByRole("link", { name: `Sign in to ${SOURCE.label}` })).toHaveAttribute(
    "href",
    setupPath({ plugin: SOURCE.id }),
  );
});
