/** Which section a route belongs to (#1058).
 *
 *  `activeSection` is the whole of the nav's highlighting, and it is the kind of function that
 *  fails quietly: a wrong answer draws the accent on the wrong entry, which reads as "you are
 *  here" and is never an error. The cases below are the ones a prefix match gets wrong.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { SectionNav } from "./SectionNav";
import { activeSection, activeSubsection, SECTIONS } from "./sections";

describe("activeSection", () => {
  test.each([
    ["/", "sessions"],
    ["/s/claude/abc-123", "sessions"],
    ["/mission", "mission"],
    // The pre-#948 path still redirects, and the nav must not flash the wrong section for the
    // render before it lands.
    ["/pulse", "mission"],
    ["/dashboard", "ask"],
    // The map is a view OF sessions since #1069: its parent section is Sessions.
    ["/overview", "sessions"],
    // Library (#1294) owns templates, automations, checklists and playbooks — automations and
    // checklists keep their `/mission/...` URLs but are NOT the Missions section any more.
    ["/templates", "library"],
    ["/templates/new", "library"],
    ["/templates/tpl_1", "library"],
    ["/mission/automations", "library"],
    ["/mission/automations/abc123", "library"],
    ["/mission/checklists", "library"],
    ["/library/playbooks", "library"],
  ])("%s → %s", (path, expected) => {
    expect(activeSection(path)).toBe(expected);
  });

  // `/ask` is only a redirect since #1294: Ask is the sidebar, not a destination.
  test.each([["/settings"], ["/settings/security"], ["/nonsense"], ["/ask"]])(
    "%s highlights NOTHING",
    (path) => {
      // Settings is a route but not a work section. Highlighting a section the operator is not in
      // is a worse answer than highlighting none.
      expect(activeSection(path)).toBeNull();
    },
  );

  test.each([
    ["/", "sessions"],
    ["/s/claude/abc-123", "sessions"],
    ["/overview", "map"],
    // #1294: Dashboard has no sub-menu; Ask is the sidebar.
    ["/dashboard", null],
    ["/ask", null],
    ["/templates", "templates"],
    ["/templates/tpl_1", "templates"],
    ["/mission/checklists", "checklists"],
    ["/library/playbooks", "playbooks"],
    ["/mission", null],
    ["/overviewer", null],
    // #1201: Automations owns its subtree (one automation's runs and editor live under it).
    ["/mission/automations", "automations"],
    ["/mission/automations/new", "automations"],
    ["/mission/automations/abc123/edit", "automations"],
    ["/mission/automationsx", null],
  ])("sub-entry of %s → %s (#1069)", (path, expected) => {
    expect(activeSubsection(path)).toBe(expected);
  });

  test("a path that merely STARTS with a section's name is not that section", () => {
    // `/askew` and `/missionary` are not routes today, and a `startsWith` implementation would
    // claim them — which is how the highlight ends up on the wrong entry after someone adds a
    // route that shares a prefix.
    expect(activeSection("/dashboardx")).toBeNull();
    expect(activeSection("/missionary")).toBeNull();
    expect(activeSection("/overviewer")).toBeNull();
    // …while `/templates/<id>` genuinely IS the Templates section, so the boundary is the slash.
    expect(activeSection("/templatesx")).toBeNull();
  });
});

describe("SectionNav", () => {
  test("the bar names every section, Dashboard first and Library last; the map is only in the Sessions menu (#1069, #1294)", () => {
    render(
      <MemoryRouter>
        <SectionNav
          active="ask"
          sessionsPath="/"
          onNavigate={() => {}}
        />
      </MemoryRouter>,
    );
    const top = SECTIONS.map((s) => s.label);
    expect(top).toEqual(["Dashboard", "Sessions", "Missions", "Library"]);
    const nav = screen.getByTestId("section-nav");
    expect(
      within(nav)
        .getAllByRole("link")
        .map((a) => a.querySelector(".section-nav-label")?.textContent),
    ).toEqual(top);
    // Missions carries a BETA tag (#1085) — for the eye only: its accessible name is still just
    // "Missions", and the tooltip says it in words.
    const missions = within(nav).getByRole("link", { name: "Missions" });
    expect(missions.querySelector(".section-nav-beta")).toHaveAttribute("aria-hidden", "true");
    expect(missions).toHaveAttribute("title", "Missions (beta)");
    expect(nav.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("Library holds Templates, Automations, Checklists and Playbooks; Missions and Dashboard have no menu (#1294)", async () => {
    render(
      <MemoryRouter>
        <SectionNav
          active="library"
          activeSub="checklists"
          sessionsPath="/"
          onNavigate={() => {}}
        />
      </MemoryRouter>,
    );
    const bar = screen.getByTestId("section-nav");
    expect(within(bar).queryByRole("button", { name: "Dashboard menu" })).toBeNull();
    expect(within(bar).queryByRole("button", { name: "Missions menu" })).toBeNull();
    const library = within(bar).getByRole("link", { name: "Library" });
    expect(library).toHaveAttribute("href", "/templates");
    // On a child that is not the section's own destination, the parent is current-in-set.
    expect(library).toHaveAttribute("aria-current", "true");
    await userEvent.click(
      within(bar).getByRole("button", { name: "Library menu" }),
    );
    const menu = await screen.findByRole("menu", { name: "Library menu" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((i) => [i.textContent, i.getAttribute("href")])).toEqual([
      ["Templates", "/templates"],
      ["Automations", "/mission/automations"],
      ["Checklists", "/mission/checklists"],
      ["Playbooks", "/library/playbooks"],
    ]);
    expect(items[2]).toHaveAttribute("aria-current", "page");
  });

  test("Sessions points at the last session route, not always at the landing", () => {
    // Leaving Sessions for the map and coming back must land on the session you were in. The
    // shell tracks that path; the nav only has to honour it.
    render(
      <MemoryRouter>
        <SectionNav
          active="map"
          sessionsPath="/s/claude/abc"
          onNavigate={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Sessions" })).toHaveAttribute(
      "href",
      "/s/claude/abc",
    );
  });

  test("on the map, Sessions is current-in-set and the map entry is the page (#1069)", async () => {
    // The Sessions link goes to the last SESSION, not the map, so it must not claim to be the
    // page — only that the page lives under it. The map's own entry, in the menu and in the
    // drawer, is the one that says "page".
    render(
      <MemoryRouter>
        <SectionNav
          active="sessions"
          activeSub="map"
          sessionsPath="/s/claude/abc"
          onNavigate={() => {}}
        />
      </MemoryRouter>,
    );
    const bar = screen.getByTestId("section-nav");
    expect(
      within(bar).getByRole("link", { name: "Sessions" }),
    ).toHaveAttribute("aria-current", "true");
    await userEvent.click(
      within(bar).getByRole("button", { name: "Sessions menu" }),
    );
    const menu = await screen.findByRole("menu", { name: "Sessions menu" });
    expect(
      within(menu).getByRole("menuitem", { name: "Sessions map" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(menu).getByRole("menuitem", { name: "Sessions" }),
    ).not.toHaveAttribute("aria-current");
  });

  test("choosing a sub-menu entry closes the menu AND the drawer (#1069)", async () => {
    let navigated = 0;
    render(
      <MemoryRouter>
        <SectionNav
          active="ask"
          sessionsPath="/"
          onNavigate={() => navigated++}
        />
      </MemoryRouter>,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Sessions menu" }),
    );
    await userEvent.click(
      await screen.findByRole("menuitem", { name: "Sessions map" }),
    );
    expect(navigated).toBe(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  test("no section is highlighted when the route is none of them", () => {
    render(
      <MemoryRouter>
        <SectionNav active={null} sessionsPath="/" onNavigate={() => {}} />
      </MemoryRouter>,
    );
    expect(
      screen.getByTestId("section-nav").querySelectorAll("[aria-current]"),
    ).toHaveLength(0);
  });
});
