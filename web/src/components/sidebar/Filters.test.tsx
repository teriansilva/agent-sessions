import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { FILTERS_OPEN_KEY, FiltersBar } from "./Filters";

beforeEach(() => localStorage.clear());

// The project facet lists project ENTITIES now (#445): the user's projects plus the synthetic
// Default catch-all — never folder paths.
const projectRef = (id: string, name: string, count: number) => ({
  kind: "project" as const,
  id,
  name,
  color: "",
  count,
});
const facets = {
  projects: [
    projectRef("p-1", "SampleProject", 3),
    projectRef("__default__", "Default", 2),
  ],
  engines: ["claude", "opencode"],
};
const base = { q: "", project: "", engine: "", archived: false };
const noop = () => {};

test("there is no project dropdown: the project tree is the project filter (#2187)", () => {
  render(
    <FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop}>
      <div data-testid="tree-slot" />
    </FiltersBar>,
  );
  expect(screen.queryByLabelText("Filter by project")).toBeNull();
  expect(screen.getByLabelText("Filter by agent")).toBeInTheDocument(); // shown: >1 engine
  // The tree renders INSIDE the fold, after Active/Archived.
  const toggle = screen.getByTestId("filters-toggle");
  const body = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  const slot = screen.getByTestId("tree-slot");
  expect(body).toContainElement(slot);
  const tabs = screen.getByRole("tablist", { name: "Which sessions" });
  expect(tabs.compareDocumentPosition(slot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("agent and mission share one row; Clear rides the search row (#2187)", () => {
  render(
    <FiltersBar
      filters={{ ...base, q: "x" }}
      facets={{ ...facets, missions: [{ id: "msn_1", title: "Release", count: 2 }] }}
      onChange={noop}
      onClear={noop}
    />,
  );
  const agent = screen.getByLabelText("Filter by agent");
  expect(screen.getByLabelText("Filter by mission").parentElement).toBe(agent.parentElement);
  expect(screen.getByLabelText("Clear filters").parentElement).toBe(
    screen.getByLabelText("Search sessions").parentElement,
  );
});

test("picking a mission replaces a selected cluster, so the scope stays true (#2187)", () => {
  const onChange = vi.fn();
  render(
    <FiltersBar
      filters={{ ...base, project: "p-1", cluster: "tc_0a1b2c3d4e5f" }}
      facets={{ ...facets, missions: [{ id: "msn_1", title: "Release", count: 2 }] }}
      onChange={onChange}
      onClear={noop}
    />,
  );
  fireEvent.change(screen.getByLabelText("Filter by mission"), { target: { value: "msn_1" } });
  // The project stays: "project › mission" is a scope the tree also offers.
  expect(onChange).toHaveBeenCalledWith({ mission: "msn_1", cluster: "" });
});

test("the header names the selected scope, so a folded block still says where the list is (#2187)", () => {
  const TC = "tc_0a1b2c3d4e5f";
  const scoped = {
    ...facets,
    projects: [projectRef("p-1", "SampleProject", 3)],
    missions: [{ id: "msn_1", title: "Release", count: 2 }],
    clusters: [{ id: TC, title: "Login flow", kept: false, project_id: "p-1", count: 2 }],
  };
  const bar = (filters: typeof base & { mission?: string; cluster?: string }) => (
    <FiltersBar filters={filters} facets={scoped} onChange={noop} onClear={noop} />
  );
  const { rerender } = render(bar({ ...base, project: "p-1", cluster: TC, q: "x" }));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("SampleProject › Login flow");
  // Search and agent are counted after it.
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent("Active · +1");
  rerender(bar({ ...base, project: "p-1", mission: "msn_1" }));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("SampleProject › Release");
  rerender(bar({ ...base, project: "p-1", cluster: "none" }));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("SampleProject › Not clustered");
  rerender(bar({ ...base, mission: "none" }));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("Not in a mission");
  rerender(bar(base));
  expect(screen.queryByTestId("filters-scope")).toBeNull();
});

test("a project filter the facets do not list still gets a name (Hermes, PR #2188)", () => {
  const bar = (project: string) => (
    <FiltersBar filters={{ ...base, project }} facets={facets} onChange={noop} onClear={noop} />
  );
  // A legacy saved folder path: the server still filters by it, its facets list entities only.
  const { rerender } = render(bar("/home/u/plain/"));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("plain");
  expect(screen.getByTestId("filters-scope")).toHaveAttribute("title", "plain");
  expect(screen.getByLabelText("Clear filters")).toBeInTheDocument();
  // A project id that has left the facets.
  rerender(bar("p-gone"));
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("Selected project");
});

test("agent select is hidden when only one engine exists", () => {
  render(
    <FiltersBar
      filters={base}
      facets={{ projects: [], engines: ["claude"] }}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.queryByLabelText("Filter by agent")).toBeNull();
});

test("search + archived toggle call onChange", () => {
  const onChange = vi.fn();
  render(
    <FiltersBar
      filters={base}
      facets={facets}
      onChange={onChange}
      onClear={noop}
    />,
  );
  fireEvent.change(screen.getByLabelText("Search sessions"), {
    target: { value: "foo" },
  });
  expect(onChange).toHaveBeenCalledWith({ q: "foo" });
  fireEvent.click(screen.getByRole("tab", { name: "Archived" }));
  expect(onChange).toHaveBeenCalledWith({ shared: false, archived: true });
});

test("clear button appears only when a filter is active", () => {
  const { rerender } = render(
    <FiltersBar
      filters={base}
      facets={facets}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.queryByLabelText("Clear filters")).toBeNull();
  rerender(
    <FiltersBar
      filters={{ ...base, q: "x" }}
      facets={facets}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.getByLabelText("Clear filters")).toBeInTheDocument();
});

test("the filters fold behind one header, remember it per device, and say what is in force (#1069)", () => {
  const { unmount } = render(
    <FiltersBar
      filters={{ ...base, q: "upload", archived: true }}
      facets={facets}
      onChange={noop}
      onClear={noop}
    />,
  );
  const toggle = screen.getByTestId("filters-toggle");
  // Open by default, so nothing moves for an operator who never folds it.
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByLabelText("Search sessions")).toBeVisible();
  // The folded header still says a filter is narrowing the list.
  expect(toggle).toHaveTextContent("Archived · 1 filter");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  // Folded, not unmounted: a half-typed search survives.
  expect(screen.getByLabelText("Search sessions")).not.toBeVisible();
  expect(screen.getByLabelText("Search sessions")).toHaveValue("upload");
  expect(localStorage.getItem(FILTERS_OPEN_KEY)).toBe("0");
  unmount();
  render(
    <FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop} />,
  );
  expect(screen.getByTestId("filters-toggle")).toHaveAttribute(
    "aria-expanded",
    "false",
  );
});

// ---- the Shared tab (#2198) ----

const bar = (
  filters: Parameters<typeof FiltersBar>[0]["filters"],
  sharedCount: number,
  onChange: Parameters<typeof FiltersBar>[0]["onChange"] = noop,
  onClear: () => void = noop,
) => (
  <FiltersBar
    filters={filters}
    facets={{ ...facets, missions: [{ id: "msn_1", title: "Release", count: 2 }] }}
    onChange={onChange}
    onClear={onClear}
    sharedCount={sharedCount}
  >
    <div data-testid="tree-slot" />
  </FiltersBar>
);
const tabNames = () => screen.getAllByRole("tab").map((t) => t.textContent);

test("the Shared tab is offered while a session is shared, and while it is selected (#2198)", () => {
  const { rerender } = render(bar(base, 0));
  expect(tabNames()).toEqual(["Active", "Archived"]);
  rerender(bar(base, 3));
  expect(tabNames()).toEqual(["Active", "Archived", "Shared 3"]);
  expect(screen.getByRole("tab", { name: "Shared 3" })).toHaveAttribute("aria-selected", "false");
  rerender(bar(base, 47));
  expect(screen.getByTestId("shared-count")).toHaveTextContent("47");
  // Selected at a count of 0: the tab stays, without a number.
  rerender(bar({ ...base, shared: true }, 0));
  expect(tabNames()).toEqual(["Active", "Archived", "Shared"]);
  expect(screen.getByRole("tab", { name: "Shared" })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByTestId("shared-count")).toBeNull();
});

test("the scope model: Shared never touches the server's archived flag (#2198)", () => {
  const onChange = vi.fn();
  const { rerender } = render(bar({ ...base, archived: true }, 2, onChange));
  expect(screen.getByRole("tab", { name: "Archived" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Shared 2" }));
  expect(onChange).toHaveBeenLastCalledWith({ shared: true });
  rerender(bar({ ...base, archived: true, shared: true }, 2, onChange));
  // Exactly one tab is selected: Shared, though `archived` is still set underneath.
  expect(
    screen.getAllByRole("tab").map((t) => t.getAttribute("aria-selected")),
  ).toEqual(["false", "false", "true"]);
  fireEvent.click(screen.getByRole("tab", { name: "Active" }));
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: false });
  fireEvent.click(screen.getByRole("tab", { name: "Archived" }));
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: true });
});

test("on Shared the search is its own, and the local filters are not shown (#2198)", () => {
  const onChange = vi.fn();
  const onClear = vi.fn();
  const local = { ...base, q: "deploy", project: "p-1", engine: "claude", mission: "msn_1" };
  const { rerender } = render(bar({ ...local, shared: true }, 3, onChange, onClear));
  expect(screen.queryByLabelText("Search sessions")).toBeNull();
  expect(screen.queryByLabelText("Filter by agent")).toBeNull();
  expect(screen.queryByLabelText("Filter by mission")).toBeNull();
  expect(screen.queryByLabelText("Clear filters")).toBeNull();
  expect(screen.queryByTestId("filters-scope")).toBeNull();
  // The tree is kept (its open branches survive) but not shown.
  expect(screen.getByTestId("tree-slot")).not.toBeVisible();
  // The local search text never shows in the Shared search.
  const search = screen.getByLabelText("Search shared sessions");
  expect(search).toHaveValue("");
  expect(screen.queryByLabelText("Clear search")).toBeNull();
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent(/Shared$/);
  fireEvent.change(search, { target: { value: "gpu" } });
  expect(onChange).toHaveBeenLastCalledWith({ sharedQ: "gpu" });

  rerender(bar({ ...local, shared: true, sharedQ: "gpu" }, 3, onChange, onClear));
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent("Shared · 1 filter");
  fireEvent.click(screen.getByLabelText("Clear search"));
  // Only the Shared search: not the list's reset, no scope, no local filter.
  expect(onChange).toHaveBeenLastCalledWith({ sharedQ: "" });
  expect(onClear).not.toHaveBeenCalled();

  // Back on a local scope everything is there again, with its values.
  rerender(bar({ ...local, sharedQ: "gpu" }, 3, onChange, onClear));
  expect(screen.getByLabelText("Search sessions")).toHaveValue("deploy");
  expect(screen.getByLabelText("Filter by agent")).toHaveValue("claude");
  expect(screen.getByLabelText("Filter by mission")).toHaveValue("msn_1");
  expect(screen.getByTestId("tree-slot")).toBeVisible();
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent("Active · +2");
});

test("the tablist: one tab stop, arrows move and select, Home and End jump (#2198)", () => {
  const onChange = vi.fn();
  const { rerender } = render(bar(base, 3, onChange));
  const tab = (name: string) => screen.getByRole("tab", { name });
  expect(screen.getAllByRole("tab").map((t) => t.tabIndex)).toEqual([0, -1, -1]);
  fireEvent.keyDown(tab("Active"), { key: "ArrowRight" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: true });
  expect(tab("Archived")).toHaveFocus();
  rerender(bar({ ...base, archived: true }, 3, onChange));
  expect(screen.getAllByRole("tab").map((t) => t.tabIndex)).toEqual([-1, 0, -1]);
  fireEvent.keyDown(tab("Archived"), { key: "ArrowRight" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: true });
  expect(tab("Shared 3")).toHaveFocus();
  rerender(bar({ ...base, archived: true, shared: true }, 3, onChange));
  // It wraps, and Home / End reach the ends.
  fireEvent.keyDown(tab("Shared 3"), { key: "ArrowRight" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: false });
  fireEvent.keyDown(tab("Shared 3"), { key: "ArrowLeft" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: true });
  fireEvent.keyDown(tab("Shared 3"), { key: "Home" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: false });
  rerender(bar(base, 3, onChange));
  fireEvent.keyDown(tab("Active"), { key: "End" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: true });
  // Two tabs: the arrows wrap between them.
  rerender(bar(base, 0, onChange));
  fireEvent.keyDown(tab("Active"), { key: "ArrowLeft" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: true });
});

test("leaving the Shared tab when it then disappears puts focus on the tab now selected (#2198)", () => {
  const onChange = vi.fn();
  const { rerender } = render(bar({ ...base, shared: true }, 0, onChange));
  screen.getByRole("tab", { name: "Shared" }).focus();
  fireEvent.keyDown(screen.getByRole("tab", { name: "Shared" }), { key: "ArrowLeft" });
  expect(onChange).toHaveBeenLastCalledWith({ shared: false, archived: true });
  rerender(bar({ ...base, archived: true }, 0, onChange));
  expect(tabNames()).toEqual(["Active", "Archived"]);
  expect(screen.getByRole("tab", { name: "Archived" })).toHaveFocus();
});
