import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import { HudFrame } from "./HudFrame";

test("renders the four corner-bracket spans (accent by default)", () => {
  const { container } = render(
    <div style={{ position: "relative" }}>
      <HudFrame />
    </div>,
  );
  const cnr = container.querySelectorAll(".hud-cnr");
  expect(cnr).toHaveLength(4);
  // The four corners, and none are the hero variant by default.
  ["tl", "tr", "bl", "br"].forEach((c) =>
    expect(container.querySelector(`.hud-cnr.${c}`)).toBeTruthy(),
  );
  expect(container.querySelector(".hud-cnr.hero")).toBeNull();
});

test("hero variant marks all four brackets as hero", () => {
  const { container } = render(
    <div style={{ position: "relative" }}>
      <HudFrame hero />
    </div>,
  );
  expect(container.querySelectorAll(".hud-cnr.hero")).toHaveLength(4);
});
