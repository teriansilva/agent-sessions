import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Filters } from "../../hooks/useSessionsList";
import { ScopeTags } from "./ScopeTags";

const base: Filters = { q: "", project: "", engine: "", archived: false };
const ref = (id: string, name: string, count: number) => ({
  kind: "project" as const,
  id,
  name,
  color: "",
  count,
});
const facets = {
  projects: [ref("p-a", "Sophia", 30), ref("p-b", "chrononote", 17)],
  clusters: [
    { id: "tc_a1", title: "API review", kept: false, project_id: "p-a", count: 8 },
    { id: "tc_b1", title: "Shadow pilot", kept: false, project_id: "p-b", count: 9 },
  ],
};

function tags(filters: Filters, over: Partial<Parameters<typeof ScopeTags>[0]> = {}) {
  const onChange = vi.fn();
  const r = render(
    <ScopeTags filters={filters} facets={facets} clustersOn onChange={onChange} {...over} />,
  );
  return { onChange, ...r };
}

test("every pick is a chip with its own remove button (#2438)", () => {
  const { onChange } = tags({ ...base, projects: ["p-a"], clusters: ["tc_b1"] });
  expect(screen.getByRole("group", { name: "Projects & clusters" })).toBeInTheDocument();
  const chips = screen.getAllByTestId("scope-chip");
  expect(chips.map((c) => c.textContent)).toEqual(["Sophia", "chrononote › Shadow pilot"]);
  fireEvent.click(screen.getByRole("button", { name: "Remove chrononote › Shadow pilot" }));
  expect(onChange).toHaveBeenLastCalledWith({
    projects: ["p-a"],
    clusters: [],
    project: "",
    cluster: "",
  });
});

test("typing filters the loaded facets locally and asks nothing of the server (#2438)", () => {
  const { onChange } = tags(base);
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.focus(input);
  expect(input).toHaveAttribute("aria-expanded", "true");
  expect(screen.getAllByRole("option")).toHaveLength(4);
  fireEvent.change(input, { target: { value: "chr" } });
  // chrononote, and its cluster by its project's name.
  expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
    "chrononote17",
    "chrononote › Shadow pilot9",
  ]);
  expect(onChange).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: "zzz" } });
  expect(screen.getByText("No project or cluster matches “zzz”.")).toBeInTheDocument();
  expect(onChange).not.toHaveBeenCalled();
});

test("the keyboard: arrows move the active option, Enter picks, Esc closes, focus stays (#2438)", () => {
  const { onChange } = tags(base);
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  input.focus();
  fireEvent.focus(input);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  const second = screen.getAllByRole("option")[1];
  expect(input).toHaveAttribute("aria-activedescendant", second.id);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onChange).toHaveBeenLastCalledWith({
    projects: ["p-b"],
    clusters: [],
    project: "",
    cluster: "",
  });
  expect(document.activeElement).toBe(input);
  fireEvent.keyDown(input, { key: "Escape" });
  expect(input).toHaveAttribute("aria-expanded", "false");
});

test("a picked option says so, and Enter on it unpicks (#2438)", () => {
  const { onChange } = tags({ ...base, projects: ["p-a"] });
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.focus(input);
  expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onChange).toHaveBeenLastCalledWith({
    projects: [],
    clusters: [],
    project: "",
    cluster: "",
  });
});

test("Backspace on an empty input removes the last chip (#2438)", () => {
  const { onChange } = tags({ ...base, projects: ["p-a", "p-b"] });
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.keyDown(input, { key: "Backspace" });
  expect(onChange).toHaveBeenLastCalledWith({
    projects: ["p-a"],
    clusters: [],
    project: "",
    cluster: "",
  });
});

test("a pick the facets no longer list keeps an honest, removable name (#2438)", () => {
  const { rerender, onChange } = tags({ ...base, projects: ["p-gone"], clusters: ["tc_a1"] });
  expect(screen.getByRole("button", { name: "Remove Project no longer there" })).toBeInTheDocument();
  // Clusters unreadable: the chip keeps the title it had and says so.
  rerender(
    <ScopeTags
      filters={{ ...base, projects: ["p-gone"], clusters: ["tc_a1"] }}
      facets={{ projects: facets.projects }}
      clustersOn
      onChange={onChange}
    />,
  );
  expect(screen.getByRole("button", { name: "Remove API review (unavailable)" })).toBeInTheDocument();
  // A cluster that has gone from a readable store.
  rerender(
    <ScopeTags
      filters={{ ...base, clusters: ["tc_zz"] }}
      facets={facets}
      clustersOn
      onChange={onChange}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove Cluster no longer there" }));
  expect(onChange).toHaveBeenLastCalledWith({
    projects: [],
    clusters: [],
    project: "",
    cluster: "",
  });
});

test("Not clustered is one chip; with clusters off no cluster is offered (#2438)", () => {
  tags({ ...base, projects: ["p-a"], cluster: "none" });
  expect(screen.getAllByTestId("scope-chip").map((c) => c.textContent)).toEqual([
    "Sophia › Not clustered",
  ]);
  const input = screen.getAllByRole("combobox", { name: "Add a project or cluster" })[0];
  fireEvent.focus(input);
  expect(screen.getAllByRole("option").length).toBe(4);
  document.body.innerHTML = "";
  tags(base, { clustersOn: false });
  const off = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.focus(off);
  expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
    "Sophia30",
    "chrononote17",
  ]);
});

test("the listbox exists only while open: a hidden one would read as a surface over HQ (#2438)", () => {
  tags(base);
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  expect(document.querySelector('[role="listbox"]')).toBeNull();
  expect(input).not.toHaveAttribute("aria-controls");
  fireEvent.focus(input);
  expect(screen.getByRole("listbox", { name: "Projects and clusters" })).toBeInTheDocument();
  expect(input).toHaveAttribute("aria-controls", screen.getByRole("listbox").id);
  fireEvent.keyDown(input, { key: "Escape" });
  expect(document.querySelector('[role="listbox"]')).toBeNull();
});

test("picking a cluster records its project with the pick (#2438 review)", () => {
  const { onChange } = tags(base);
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Shadow" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onChange).toHaveBeenLastCalledWith({
    projects: [],
    clusters: ["tc_b1"],
    project: "",
    cluster: "",
    clusterParents: { tc_b1: "p-b" },
  });
});

test("adding a tag clears a mission; removing a chip leaves it (#2438 review)", () => {
  const withMission: Filters = { ...base, projects: ["p-a"], mission: "msn_1" };
  const { onChange, unmount } = tags(withMission);
  const input = screen.getByRole("combobox", { name: "Add a project or cluster" });
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: "Shadow" } });
  fireEvent.keyDown(input, { key: "Enter" });
  // mission ∧ cluster could only be empty: the mission goes with the add.
  expect(onChange).toHaveBeenLastCalledWith(
    expect.objectContaining({ projects: ["p-a"], clusters: ["tc_b1"], mission: "" }),
  );
  unmount();
  const again = tags(withMission);
  fireEvent.click(screen.getByRole("button", { name: "Remove Sophia" }));
  expect(again.onChange).toHaveBeenLastCalledWith(
    expect.not.objectContaining({ mission: expect.anything() }),
  );
});
