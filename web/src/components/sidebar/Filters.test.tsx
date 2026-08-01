import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { FiltersBar } from "./Filters";

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
  projects: [projectRef("p-1", "Cayoo", 3), projectRef("__default__", "Default", 2)],
  engines: ["claude", "opencode"],
};
const base = { q: "", project: "", engine: "", archived: false };
const noop = () => {};

test("lists project entities (incl. Default) from facets, not folder paths (#445)", () => {
  render(<FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop} />);
  expect(screen.getByLabelText("Filter by project")).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Cayoo (3)" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Default (2)" })).toBeInTheDocument();
  expect(screen.getByLabelText("Filter by agent")).toBeInTheDocument(); // shown: >1 engine
});

test("lists empty (0-count) projects too, rendered as Name (N) (#445)", () => {
  const counted = {
    projects: [
      projectRef("p-1", "Cayoo", 3),
      projectRef("p-2", "Empty", 0),
      projectRef("__default__", "Default", 2),
    ],
    engines: ["claude"],
  };
  render(<FiltersBar filters={base} facets={counted} onChange={noop} onClear={noop} />);
  expect(screen.getByRole("option", { name: "Cayoo (3)" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Empty (0)" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Default (2)" })).toBeInTheDocument();
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
  render(<FiltersBar filters={base} facets={facets} onChange={onChange} onClear={noop} />);
  fireEvent.change(screen.getByLabelText("Search sessions"), { target: { value: "foo" } });
  expect(onChange).toHaveBeenCalledWith({ q: "foo" });
  fireEvent.click(screen.getByRole("tab", { name: "Archived" }));
  expect(onChange).toHaveBeenCalledWith({ archived: true });
});

test("clear button appears only when a filter is active", () => {
  const { rerender } = render(
    <FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop} />,
  );
  expect(screen.queryByLabelText("Clear filters")).toBeNull();
  rerender(<FiltersBar filters={{ ...base, q: "x" }} facets={facets} onChange={noop} onClear={noop} />);
  expect(screen.getByLabelText("Clear filters")).toBeInTheDocument();
});
