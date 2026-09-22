/** Which section a route belongs to (#1058).
 *
 *  `activeSection` is the whole of the nav's highlighting, and it is the kind of function that
 *  fails quietly: a wrong answer draws the accent on the wrong entry, which reads as "you are
 *  here" and is never an error. The cases below are the ones a prefix match gets wrong.
 */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { SectionNav } from "./SectionNav";
import { activeSection, SECTIONS } from "./sections";

describe("activeSection", () => {
  test.each([
    ["/", "sessions"],
    ["/s/claude/abc-123", "sessions"],
    ["/mission", "mission"],
    // The pre-#948 path still redirects, and the nav must not flash the wrong section for the
    // render before it lands.
    ["/pulse", "mission"],
    ["/ask", "ask"],
    ["/overview", "map"],
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
  test("the bar and the drawer render the SAME five sections", () => {
    // One list, two paints. The failure this prevents is a phone whose drawer names four of the
    // five icons in the bar above it.
    render(
      <MemoryRouter>
        <SectionNav active="ask" sessionsPath="/" onNavigate={() => {}} />
        <SectionNav
          active="ask"
          sessionsPath="/"
          onNavigate={() => {}}
          variant="drawer"
        />
      </MemoryRouter>,
    );
    const names = SECTIONS.map((s) => s.label);
    for (const testid of ["section-nav", "section-nav-drawer"]) {
      const nav = screen.getByTestId(testid);
      expect(
        within(nav)
          .getAllByRole("link")
          .map((a) => a.textContent),
      ).toEqual(names);
      // …and exactly one of them claims the page.
      expect(nav.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
      expect(
        within(nav).getByRole("link", { name: "Ask" }),
      ).toHaveAttribute("aria-current", "page");
    }
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

  test("no section is highlighted when the route is none of them", () => {
    render(
      <MemoryRouter>
        <SectionNav active={null} sessionsPath="/" onNavigate={() => {}} />
      </MemoryRouter>,
    );
    expect(
      screen.getByTestId("section-nav").querySelectorAll('[aria-current]'),
    ).toHaveLength(0);
  });
});
