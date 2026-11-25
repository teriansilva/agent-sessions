import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { CLOSE_ARCHIVED } from "../../lib/termCloseCodes";
import { HeadFacts } from "./HeadFacts";

// #2064: status colour is state (design.md §3). An archived session is frozen on purpose — its
// LED reads ARCHIVED, neutral and steady — never OFFLINE (red) like a refused link.

test("an archived pane's LED is neutral ARCHIVED, a refused one stays red OFFLINE", () => {
  const { rerender, container } = render(
    <HeadFacts
      engine="claude"
      status={{ kind: "rejected", reason: "session archived — read-only", code: CLOSE_ARCHIVED }}
      row={undefined}
    />,
  );
  let led = container.querySelector("[data-head-led]");
  expect(led).toHaveAttribute("data-head-led", "archived");
  expect(led).toHaveClass("idle");
  expect(led).not.toHaveClass("down");
  rerender(
    <HeadFacts engine="claude" status={{ kind: "rejected", reason: "gone" }} row={undefined} />,
  );
  led = container.querySelector("[data-head-led]");
  expect(led).toHaveAttribute("data-head-led", "offline");
  expect(led).toHaveClass("down");
  expect(screen.queryByText("ARCHIVED")).toBeNull(); // the label is the accessible name, not text
});
