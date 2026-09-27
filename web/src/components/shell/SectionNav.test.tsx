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
    ["/ask", "ask"],
    // The map is a view OF sessions since #1069: its parent section is Sessions.
    ["/overview", "sessions"],
    ["/templates", "templates"],
    ["/templates/new", "templates"],
    ["/templates/tpl_1", "templates"],
  ])("%s → %s", (path, expected) => {
    expect(activeSection(path)).toBe(expected);
  });

  test.each([["/settings"], ["/settings/security"], ["/nonsense"]])(
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
    ["/ask", null],
    ["/overviewer", null],
  ])("sub-entry of %s → %s (#1069)", (path, expected) => {
    expect(activeSubsection(path)).toBe(expected);
  });

  test("a path that merely STARTS with a section's name is not that section", () => {
    // `/askew` and `/missionary` are not routes today, and a `startsWith` implementation would
    // claim them — which is how the highlight ends up on the wrong entry after someone adds a
    // route that shares a prefix.
    expect(activeSection("/askew")).toBeNull();
    expect(activeSection("/missionary")).toBeNull();
    expect(activeSection("/overviewer")).toBeNull();
    // …while `/templates/<id>` genuinely IS the Templates section, so the boundary is the slash.
    expect(activeSection("/templatesx")).toBeNull();
  });
});

describe("SectionNav", () => {
  test("the bar names every section, Ask first; the map is only in the Sessions menu (#1069)", () => {
    render(
      <MemoryRouter>
        <SectionNav active="ask" sessionsPath="/" onNavigate={() => {}} />
      </MemoryRouter>,
    );
    const top = SECTIONS.map((s) => s.label);
    expect(top).toEqual(["Dashboard", "Sessions", "Missions", "Templates"]);
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
