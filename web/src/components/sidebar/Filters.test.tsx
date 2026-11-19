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
  const tabs = screen.getByRole("tablist", { name: "Archived filter" });
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
  expect(onChange).toHaveBeenCalledWith({ archived: true });
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
