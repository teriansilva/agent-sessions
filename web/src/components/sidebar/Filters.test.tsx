import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { FiltersBar } from "./Filters";

const folderRef = (cwd: string) => ({ kind: "folder" as const, id: cwd, name: cwd.split("/").pop() ?? cwd });
const facets = {
  projects: [folderRef("/home/m/claude"), folderRef("/tmp/x")],
  engines: ["claude", "opencode"],
};
const base = { q: "", project: "", engine: "", archived: false };
const noop = () => {};

test("renders project + agent options from facets", () => {
  render(<FiltersBar filters={base} facets={facets} onChange={noop} onClear={noop} />);
  expect(screen.getByLabelText("Filter by project")).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "~/claude" })).toBeInTheDocument();
  expect(screen.getByLabelText("Filter by agent")).toBeInTheDocument(); // shown: >1 engine
});

test("a facet ref with a count renders as Name (N) (#361)", () => {
  const counted = {
    projects: [
      { kind: "project" as const, id: "p-1", name: "Cayoo", color: "", count: 3 },
      { ...folderRef("/home/m/claude"), count: 2 },
    ],
    engines: ["claude"],
  };
  render(<FiltersBar filters={base} facets={counted} onChange={noop} onClear={noop} />);
  expect(screen.getByRole("option", { name: "Cayoo (3)" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "~/claude (2)" })).toBeInTheDocument();
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
