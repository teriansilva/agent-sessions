import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, onTestFinished, test, vi } from "vitest";
import Memory from "./Memory";

// The graph is ReactFlow (needs ResizeObserver, absent in jsdom) and is covered in a real browser by
// e2e/memory.spec.ts; here it is a plain list of the note names it was given.
vi.mock("../components/memory/MemoryGraph", () => ({
  default: ({ notes }: { notes: { slug: string; name: string }[] }) => (
    <ul data-testid="memory-graph">{notes.map(n => <li key={n.slug}>{n.name}</li>)}</ul>
  ),
}));
vi.mock("../lib/api", async importOriginal => {
  const real = await importOriginal<typeof import("../lib/api")>();
  return {
    ...real,
    api: {
      memoryProjects: vi.fn(),
      memory: vi.fn(),
      ingestMemory: vi.fn(),
      setMemoryEnabled: vi.fn(),
      clearMemory: vi.fn(),
      editMemoryNote: vi.fn(),
      memoryExportUrl: (pid: string) => `/api/memory/${pid}/export`,
      memoryImportPreview: vi.fn(async () => ({ sources: [], last: null, held: [] })),
      importMemory: vi.fn(),
    },
  };
});
import { api } from "../lib/api";
import { ConfigCtx } from "../app/config";
import type { AppConfig } from "../types/api";

const view = {
  project: { id: "p-1", name: "demo", color: "" },
  enabled: false,
  notes: [],
  pending: [],
  refused: [],
  quarantine: [],
  withheld: { notes: 0, reason: "" },
  instructions: "If `$BATTLELAB_MEMORY` is set, read it.",
  limits: { note_bytes: 4096, notes: 200, inbox_files: 20 },
};

function renderPage() {
  const router = createMemoryRouter([{ path: "/library/memory", element: <Memory /> }], {
    initialEntries: ["/library/memory"],
  });
  return render(<RouterProvider router={router} />);
}

describe("Memory page", () => {
  beforeEach(() => vi.clearAllMocks());

  test("no projects: says memory belongs to a project", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [] });
    renderPage();
    expect(await screen.findByText(/Memory belongs to a project/)).toBeInTheDocument();
    expect(api.memory).not.toHaveBeenCalled();
  });

  test("off by default: explains the two variables and offers to turn it on", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({
      projects: [{ id: "p-1", name: "demo", color: "", enabled: false, notes: 0, pending: 0, quarantined: 0, last_write: null }],
    });
    vi.mocked(api.ingestMemory).mockResolvedValue({});
    vi.mocked(api.memory).mockResolvedValue(view as never);
    renderPage();
    expect(await screen.findByRole("button", { name: "Turn memory on" })).toBeInTheDocument();
    expect(screen.getAllByText(/BATTLELAB_MEMORY_INBOX/).length).toBeGreaterThan(0);
    expect(screen.getByRole("switch", { name: "Shared memory for demo" })).toHaveAttribute("aria-checked", "false");
    expect(api.memory).toHaveBeenCalledWith("p-1");
  });

  test("a failed load is an alert, not an endless spinner", async () => {
    vi.mocked(api.memoryProjects).mockRejectedValue(new Error("the server is down"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("the server is down");
  });
});


function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}

const two = {
  projects: [
    { id: "p-a", name: "alpha", color: "", enabled: true, notes: 1, pending: 0, quarantined: 0, last_write: null },
    { id: "p-b", name: "beta", color: "", enabled: true, notes: 1, pending: 0, quarantined: 0, last_write: null },
  ],
};
const note = (slug: string, name: string) => ({
  slug, name, description: "", type: "finding", body: name, size: 1, created: 0, updated: 0,
  session: { key: "", engine: "", kind: "terminal", resolved: false }, links: [],
});
const viewOf = (id: string, name: string, notes = [note(`${name}-note`, `${name} note`)]) => ({
  ...view, enabled: true, project: { id, name, color: "" }, notes,
});

function renderAt(path: string, config: AppConfig | null = null) {
  const router = createMemoryRouter([{ path: "/library/memory", element: <Memory /> }], {
    initialEntries: [path],
  });
  render(
    <ConfigCtx.Provider value={config}>
      <RouterProvider router={router} />
    </ConfigCtx.Provider>,
  );
  return router;
}

describe("Memory page — one project never shows another's data (#1453 review)", () => {
  beforeEach(() => vi.clearAllMocks());

  test("a late response for A, settling after switching to B, never shows under B", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    const a = deferred<unknown>();
    const b = deferred<unknown>();
    vi.mocked(api.memory).mockImplementation(id => (id === "p-a" ? a.promise : b.promise) as never);
    const router = renderAt("/library/memory?project=p-a");
    await waitFor(() => expect(api.memory).toHaveBeenCalledWith("p-a"));
    await act(() => router.navigate("/library/memory?project=p-b"));
    await waitFor(() => expect(api.memory).toHaveBeenCalledWith("p-b"));
    await act(async () => a.resolve(viewOf("p-a", "alpha")));
    expect(screen.queryByText("alpha note")).toBeNull();
    await act(async () => b.resolve(viewOf("p-b", "beta")));
    expect(await screen.findAllByText("beta note")).not.toHaveLength(0);
    expect(screen.queryByText("alpha note")).toBeNull();
  });

  test("a mutation on A that settles after B is selected refreshes A, never B's view", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    vi.mocked(api.memory).mockImplementation(async id =>
      (id === "p-a" ? viewOf("p-a", "alpha") : viewOf("p-b", "beta")) as never,
    );
    const toggled = deferred<unknown>();
    vi.mocked(api.setMemoryEnabled).mockReturnValue(toggled.promise as never);
    const router = renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("switch", { name: "Shared memory for alpha" }));
    // While it runs, the project rail does not switch.
    expect(screen.getByRole("button", { name: /beta/ })).toBeDisabled();
    await act(() => router.navigate("/library/memory?project=p-b"));
    expect(await screen.findAllByText("beta note")).not.toHaveLength(0);
    vi.mocked(api.memory).mockClear();
    await act(async () => toggled.resolve({ enabled: false }));
    await waitFor(() => expect(api.memory).toHaveBeenCalledWith("p-a"));
    expect(api.memory).not.toHaveBeenCalledWith("p-b");
    expect(screen.getAllByText("beta note")).not.toHaveLength(0);
    expect(screen.queryByText(/Memory is off/)).toBeNull(); // A's message stays with A
  });

  test("memory off keeps held items in the trays", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue({
      ...viewOf("p-a", "alpha", []),
      enabled: false,
      quarantine: [{ id: 3, filename: "q.md", reason: "looks like a credential", size: 1, at: 0, masked: "████████ (withheld)", session: note("x", "x").session }],
    } as never);
    renderAt("/library/memory?project=p-a");
    expect(await screen.findByRole("button", { name: "Turn memory on" })).toBeInTheDocument();
    expect(screen.getByLabelText("Quarantined q.md")).toBeInTheDocument();
  });

  test("an open page takes in new notes every 30 s while visible", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
      vi.mocked(api.ingestMemory).mockResolvedValue({});
      vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha") as never);
      renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
      await waitFor(() => expect(api.ingestMemory).toHaveBeenCalledTimes(1));
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      await waitFor(() => expect(api.ingestMemory).toHaveBeenCalledTimes(2));
      expect(vi.mocked(api.memory).mock.calls.length).toBeGreaterThanOrEqual(3);
    } finally {
      vi.useRealTimers();
    }
  });

  test("a pass that answers with the fresh view is shown without a second request (#1501)", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha") as never);
    vi.mocked(api.ingestMemory).mockResolvedValue({ notes: 1, view: viewOf("p-a", "alpha", [note("fresh", "fresh from the pass")]) } as never);
    renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
    expect(await screen.findAllByText("fresh from the pass")).not.toHaveLength(0);
    // The mount load only: the pass's own view replaced it, with no GET after the POST.
    expect(api.memory).toHaveBeenCalledTimes(1);
  });
});


describe("Memory page — destructive actions and filters (#1453 review 2)", () => {
  beforeEach(() => vi.clearAllMocks());

  test("a Clear dialog opened for A is dropped when B is selected, and never clears B", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    vi.mocked(api.memory).mockImplementation(async id =>
      (id === "p-a" ? viewOf("p-a", "alpha") : viewOf("p-b", "beta")) as never,
    );
    vi.mocked(api.clearMemory).mockResolvedValue(undefined as never);
    const router = renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("button", { name: "Clear…" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await act(() => router.navigate("/library/memory?project=p-b")); // browser history, not the rail
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.clearMemory).not.toHaveBeenCalled();
  });

  test("Clear stays disabled until the project's name is typed, then clears THAT project", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha") as never);
    vi.mocked(api.clearMemory).mockResolvedValue(undefined as never);
    renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("button", { name: "Clear…" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Clear memory" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByRole("textbox"), "alph");
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByRole("textbox"), "a");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    await waitFor(() => expect(api.clearMemory).toHaveBeenCalledWith("p-a"));
  });

  test("the list filters by type and by agent", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    const n1 = { ...note("one", "first finding"), type: "finding", session: { key: "x:1", engine: "codexx", kind: "terminal", resolved: true } };
    const n2 = { ...note("two", "a decision"), type: "decision", session: { key: "y:2", engine: "claudex", kind: "api", resolved: true } };
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha", [n1, n2] as never) as never);
    renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    expect(await screen.findByRole("button", { name: /first finding/ })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "decision");
    expect(screen.queryByRole("button", { name: /first finding/ })).toBeNull();
    expect(screen.getByRole("button", { name: /a decision/ })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "");
    await userEvent.selectOptions(screen.getByLabelText("Filter by agent"), "codexx");
    expect(screen.getByRole("button", { name: /first finding/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /a decision/ })).toBeNull();
  });

  test("an agent filter chosen in A does not hide B's notes", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    const a1 = { ...note("one", "alpha finding"), session: { key: "x:1", engine: "codexx", kind: "terminal", resolved: true } };
    const a2 = { ...note("two", "alpha decision"), session: { key: "y:2", engine: "claudex", kind: "api", resolved: true } };
    const b1 = { ...note("three", "beta finding"), session: { key: "z:3", engine: "claudex", kind: "api", resolved: true } };
    const b2 = { ...note("four", "beta codex note"), session: { key: "w:4", engine: "codexx", kind: "terminal", resolved: true } };
    vi.mocked(api.memory).mockImplementation(async id =>
      (id === "p-a" ? viewOf("p-a", "alpha", [a1, a2] as never) : viewOf("p-b", "beta", [b1, b2] as never)) as never,
    );
    const router = renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await screen.findByRole("button", { name: /alpha finding/ });
    await userEvent.selectOptions(screen.getByLabelText("Filter by agent"), "codexx");
    expect(screen.queryByRole("button", { name: /alpha decision/ })).toBeNull();
    await act(() => router.navigate("/library/memory?project=p-b"));
    if (!screen.queryByLabelText("Filter by agent")) await userEvent.click(await screen.findByRole("button", { name: "List" }));
    expect(await screen.findByRole("button", { name: /beta finding/ })).toBeInTheDocument();
    expect((screen.getByLabelText("Filter by agent") as HTMLSelectElement).value).toBe("");
  });

  test("an agent filter whose last note goes away stops filtering", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => { vi.useRealTimers(); });
    vi.mocked(api.ingestMemory).mockResolvedValue({});
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    const n1 = { ...note("one", "first finding"), session: { key: "x:1", engine: "codexx", kind: "terminal", resolved: true } };
    const n2 = { ...note("two", "a decision"), session: { key: "y:2", engine: "claudex", kind: "api", resolved: true } };
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha", [n1, n2] as never) as never);
    renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await screen.findByRole("button", { name: /first finding/ });
    await userEvent.selectOptions(screen.getByLabelText("Filter by agent"), "codexx");
    expect(screen.queryByRole("button", { name: /a decision/ })).toBeNull();
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha", [n2] as never) as never);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(await screen.findByRole("button", { name: /a decision/ })).toBeInTheDocument();
    expect((screen.getByLabelText("Filter by agent") as HTMLSelectElement).value).toBe("");
  });

  test("a Save in flight locks the form, so no later edit is silently discarded", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha") as never);
    const saved = deferred<unknown>();
    vi.mocked(api.editMemoryNote).mockReturnValue(saved.promise as never);
    renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await userEvent.click(await screen.findByRole("button", { name: /alpha note/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const name = screen.getByRole("textbox", { name: "Name" });
    await userEvent.clear(name);
    await userEvent.type(name, "first");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    for (const field of ["Name", "Description", "Body"]) expect(screen.getByRole("textbox", { name: field })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Type" })).toBeDisabled();
    await userEvent.type(name, " second");
    expect(name).toHaveValue("first");
    await act(async () => saved.resolve({}));
    expect(api.editMemoryNote).toHaveBeenCalledTimes(1);
    expect(api.editMemoryNote).toHaveBeenCalledWith("p-a", "alpha-note", { name: "first" });
  });

  test("a Save settling after another note's editor opened leaves that editor and its edit alone", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue(
      viewOf("p-a", "alpha", [note("one", "first note"), note("two", "second note")] as never) as never,
    );
    const saved = deferred<unknown>();
    vi.mocked(api.editMemoryNote).mockReturnValue(saved.promise as never);
    renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await userEvent.click(await screen.findByRole("button", { name: /first note/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), " A");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(screen.getByRole("button", { name: /second note/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), " B");
    await act(async () => saved.resolve({}));
    expect(await screen.findByText("Saved “first note A”.")).toBeInTheDocument(); // A's Save finished
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("second note B");
    expect(api.editMemoryNote).toHaveBeenCalledWith("p-a", "one", { name: "first note A" });
  });

  test("a Save settling after the project changed leaves the new project's editor alone", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    vi.mocked(api.memory).mockImplementation(async id =>
      (id === "p-a" ? viewOf("p-a", "alpha") : viewOf("p-b", "beta")) as never,
    );
    const saved = deferred<unknown>();
    vi.mocked(api.editMemoryNote).mockReturnValue(saved.promise as never);
    const router = renderAt("/library/memory?project=p-a", { csrf: "x" } as unknown as AppConfig);
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await userEvent.click(await screen.findByRole("button", { name: /alpha note/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), " A");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await act(() => router.navigate("/library/memory?project=p-b"));
    if (!screen.queryByRole("button", { name: /beta note/ })) await userEvent.click(await screen.findByRole("button", { name: "List" }));
    await userEvent.click(await screen.findByRole("button", { name: /beta note/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), " B");
    await act(async () => saved.resolve({}));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeEnabled()); // A's Save finished
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("beta note B");
  });

  test("an import preview for A that settles after B is selected never shows under B (#1471)", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue(two as never);
    vi.mocked(api.memory).mockImplementation(async id =>
      (id === "p-a" ? viewOf("p-a", "alpha") : viewOf("p-b", "beta")) as never,
    );
    const late = deferred<unknown>();
    const store = (files: number) => ({
      sources: [{ id: "s", engine: "claude", label: "Claude Code", path: "~/m", relation: "this folder", files, over_agent_size: 0, over_cap: 0, beyond_max_files: 0, state: "ok", reason: "" }],
      last: null, held: [],
    });
    vi.mocked(api.memoryImportPreview).mockImplementation(id => (id === "p-a" ? late.promise : Promise.resolve(store(7))) as never);
    const router = renderAt("/library/memory?project=p-a");
    await waitFor(() => expect(api.memoryImportPreview).toHaveBeenCalledWith("p-a"));
    await act(() => router.navigate("/library/memory?project=p-b"));
    expect(await screen.findByText(/keeps/)).toHaveTextContent("Claude Code keeps 7 notes in 1 place");
    await act(async () => late.resolve(store(999)));
    expect(screen.getByText(/keeps/)).toHaveTextContent("7 notes");
    expect(screen.queryByText(/999/)).toBeNull();
  });

  test("an imported note says it was imported, not that its session is unresolved", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    const imported = { ...note("imp", "from claude memory"), session: { key: "", engine: "claudex", kind: "import", resolved: false } };
    vi.mocked(api.memory).mockResolvedValue(viewOf("p-a", "alpha", [imported] as never) as never);
    renderAt("/library/memory?project=p-a");
    await userEvent.click(await screen.findByRole("button", { name: "List" }));
    const row = await screen.findByRole("button", { name: /from claude memory/ });
    expect(row).toHaveTextContent("imported");
    expect(row).not.toHaveTextContent("unresolved");
  });

  test("notes held back for a secret are counted, never shown", async () => {
    vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [two.projects[0]] } as never);
    vi.mocked(api.memory).mockResolvedValue({
      ...viewOf("p-a", "alpha"), withheld: { notes: 2, reason: "they hold a value now stored as a secret" },
    } as never);
    renderAt("/library/memory?project=p-a");
    expect(await screen.findByRole("note")).toHaveTextContent(
      "2 notes are not shown because they hold a value now stored as a secret.",
    );
  });
});
