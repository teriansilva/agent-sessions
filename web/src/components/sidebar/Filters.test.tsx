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
      facets={{
        ...facets,
        missions: [{ id: "msn_1", title: "Release", count: 2 }],
        clusters: [
          { id: "tc_0a1b2c3d4e5f", title: "Login flow", kept: false, project_id: "p-1", count: 2 },
        ],
      }}
      onChange={onChange}
      onClear={noop}
    />,
  );
  fireEvent.change(screen.getByLabelText("Filter by mission"), { target: { value: "msn_1" } });
  // The cluster pick gives way to its project (a mission-held session is never in a cluster), so
  // "project › mission" stays true; the canonical arrays are written, the old scalars cleared.
  expect(onChange).toHaveBeenCalledWith({
    mission: "msn_1",
    projects: ["p-1"],
    clusters: [],
    project: "",
    cluster: "",
  });
});

test("the mission dropdown keeps the project picks; a cluster pick gives way to its project (#2438)", () => {
  const onChange = vi.fn();
  render(
    <FiltersBar
      filters={{ ...base, projects: ["p-1", "p-2"], clusters: ["tc_0a1b2c3d4e5f"] }}
      facets={{ ...facets, missions: [{ id: "msn_1", title: "Release", count: 2 }] }}
      onChange={onChange}
      onClear={noop}
    />,
  );
  fireEvent.change(screen.getByLabelText("Filter by mission"), { target: { value: "msn_1" } });
  expect(onChange).toHaveBeenCalledWith({
    mission: "msn_1",
    // tc_0a1b2c3d4e5f is not in these facets and there is no saved scalar project: it goes.
    projects: ["p-1", "p-2"],
    clusters: [],
    project: "",
    cluster: "",
  });
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
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("Project no longer there");
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

// ---- Filter by tag (#2338) -----------------------------------------------------------------

const tagFacets = {
  ...facets,
  tags: [
    { tag: "Bug", key: "bug", count: 2 },
    { tag: "🔥 release", key: "🔥 release", count: 1 },
  ],
};

test("the tag dropdown lists every tag with its count, on its own row (#2338)", () => {
  const onChange = vi.fn();
  render(<FiltersBar filters={base} facets={tagFacets} onChange={onChange} onClear={noop} />);
  const select = screen.getByLabelText("Filter by tag") as HTMLSelectElement;
  expect([...select.options].map((o) => o.textContent)).toEqual([
    "All tags",
    "Bug (2)",
    "🔥 release (1)",
  ]);
  // Not crammed into the agent/mission row.
  expect(select.parentElement).not.toBe(screen.getByLabelText("Filter by agent").parentElement);
  fireEvent.change(select, { target: { value: "🔥 release" } });
  expect(onChange).toHaveBeenCalledWith({ tag: "🔥 release" });
});

test("the tag dropdown is offered only while tags exist or one is selected (#2338)", () => {
  const { rerender } = render(
    <FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop} />,
  );
  expect(screen.queryByLabelText("Filter by tag")).toBeNull(); // older server: no facet
  rerender(
    <FiltersBar filters={base} facets={{ ...facets, tags: [] }} onChange={noop} onClear={noop} />,
  );
  expect(screen.queryByLabelText("Filter by tag")).toBeNull(); // nothing tagged
  // A selected tag no session carries any more stays listed, so the control shows its value.
  rerender(
    <FiltersBar
      filters={{ ...base, tag: "gone" }}
      facets={{ ...facets, tags: [] }}
      onChange={noop}
      onClear={noop}
    />,
  );
  const select = screen.getByLabelText("Filter by tag") as HTMLSelectElement;
  expect(select).toHaveValue("gone");
  expect([...select.options].map((o) => o.value)).toEqual(["", "gone"]);
});

test("a selected tag counts as a filter: Clear shows and the folded header says so (#2338)", () => {
  const onClear = vi.fn();
  render(
    <FiltersBar
      filters={{ ...base, tag: "bug" }}
      facets={tagFacets}
      onChange={noop}
      onClear={onClear}
    />,
  );
  // The stored value is the group key, so it selects the "Bug" option exactly.
  const select = screen.getByLabelText("Filter by tag") as HTMLSelectElement;
  expect(select).toHaveValue("bug");
  expect(select.selectedOptions[0].textContent).toBe("Bug (2)");
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent("Active · 1 filter");
  fireEvent.click(screen.getByLabelText("Clear filters"));
  expect(onClear).toHaveBeenCalled();
});

test("the tag dropdown is a local filter: not shown on Shared (#2338)", () => {
  render(
    <FiltersBar
      filters={{ ...base, tag: "bug", shared: true }}
      facets={tagFacets}
      onChange={noop}
      onClear={noop}
      sharedCount={2}
    />,
  );
  expect(screen.queryByLabelText("Filter by tag")).toBeNull();
  // …nor counted there: the folded header names only the Shared search.
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent(/Shared$/);
});

test("a group whose spelling differs from the saved key is still ONE selected option (#2338 review)", () => {
  // The server folds `straße` and `STRASSE` into the key `strasse` (Python casefold); the browser
  // only compares keys, so it never shows the same group twice.
  render(
    <FiltersBar
      filters={{ ...base, tag: "strasse" }}
      facets={{ ...facets, tags: [{ tag: "STRASSE", key: "strasse", count: 2 }] }}
      onChange={noop}
      onClear={noop}
    />,
  );
  const select = screen.getByLabelText("Filter by tag") as HTMLSelectElement;
  expect([...select.options].map((o) => o.textContent)).toEqual(["All tags", "STRASSE (2)"]);
  expect(select).toHaveValue("strasse");
});

test("a long or emoji tag keeps the select inside the bar (#2338)", () => {
  const long = { tag: "🔥🔥 a-very-long-tag-name-xx", key: "🔥🔥 a-very-long-tag-name-xx", count: 12 };
  render(
    <FiltersBar
      filters={{ ...base, tag: long.tag }}
      facets={{ ...facets, tags: [long] }}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.getByLabelText("Filter by tag")).toHaveValue(long.tag);
  expect(screen.getByRole("option", { name: `${long.tag} (12)` })).toBeInTheDocument();
});

// ---- Started by (#2437) ----

const withOrigins = { ...facets, origins: { operator: 12, automation: 35 } };

test("Started by is a radio group with counts; arrows move and pick (#2437)", () => {
  const onChange = vi.fn();
  render(<FiltersBar filters={base} facets={withOrigins} onChange={onChange} onClear={noop} />);
  const group = screen.getByRole("radiogroup", { name: "Started by" });
  expect(group).toBeInTheDocument();
  const you = screen.getByRole("radio", { name: /You 12/ });
  expect(you).toHaveAttribute("aria-checked", "true");
  expect(you).toHaveAttribute("tabindex", "0");
  expect(screen.getByRole("radio", { name: /Auto 35/ })).toHaveAttribute("tabindex", "-1");
  expect(screen.getByRole("radio", { name: /All 47/ })).toBeInTheDocument();
  fireEvent.keyDown(you, { key: "ArrowRight" });
  expect(onChange).toHaveBeenLastCalledWith({ origin: "automation" });
  fireEvent.keyDown(you, { key: "End" });
  expect(onChange).toHaveBeenLastCalledWith({ origin: "all" });
  fireEvent.click(screen.getByRole("radio", { name: /Auto/ }));
  expect(onChange).toHaveBeenLastCalledWith({ origin: "automation" });
});

test("Started by is not offered when no automation started a session in scope (#2437)", () => {
  render(
    <FiltersBar
      filters={base}
      facets={{ ...facets, origins: { operator: 5, automation: 0 } }}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.queryByRole("radiogroup", { name: "Started by" })).toBeNull();
});

test("off the default, Started by counts as a filter, Clear resets it, and unknown counts drop (#2437)", () => {
  render(
    <FiltersBar filters={{ ...base, origin: "automation" }} facets={facets} onChange={noop} onClear={noop} />,
  );
  // facets.origins absent (store unreadable): still offered so the operator can get back to You.
  const auto = screen.getByRole("radio", { name: "Auto" });
  expect(auto).toHaveAttribute("aria-checked", "true");
  expect(screen.getByLabelText("Clear filters")).toBeInTheDocument();
  expect(screen.getByTestId("filters-toggle")).toHaveTextContent("1 filter");
});

test("Started by is not shown on Shared (#2437)", () => {
  render(
    <FiltersBar
      filters={{ ...base, shared: true }}
      facets={withOrigins}
      onChange={noop}
      onClear={noop}
      sharedCount={2}
    />,
  );
  expect(screen.queryByRole("radiogroup", { name: "Started by" })).toBeNull();
});

test("the header lists every project and cluster pick (#2438)", () => {
  const TC = "tc_0a1b2c3d4e5f";
  const scoped = {
    ...facets,
    projects: [projectRef("p-1", "SampleProject", 3), projectRef("p-2", "Other", 1)],
    clusters: [{ id: TC, title: "Login flow", kept: false, project_id: "p-1", count: 2 }],
  };
  render(
    <FiltersBar
      filters={{ ...base, projects: ["p-2"], clusters: [TC] }}
      facets={scoped}
      onChange={noop}
      onClear={noop}
    />,
  );
  expect(screen.getByTestId("filters-scope")).toHaveTextContent("Other, SampleProject › Login flow");
  expect(screen.getByLabelText("Clear filters")).toBeInTheDocument();
});
