import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { presenceTitle, presentChip, presentLabel } from "../../lib/shareFormat";
import { Avatar } from "./Avatar";
import { operatorAvatarSpec } from "./avatarSpec";

const disc = (c: HTMLElement) => c.querySelector("[data-avatar]") as HTMLElement;

describe("Avatar (#1931)", () => {
  test("an operator without an image is their first initial on the accent", () => {
    expect(operatorAvatarSpec(null, "marcus")).toEqual({ kind: "accent", initial: "M" });
    expect(operatorAvatarSpec(undefined, "night owl")).toEqual({ kind: "accent", initial: "N" });
    expect(operatorAvatarSpec(null, null)).toEqual({ kind: "accent", initial: "Y" });
  });

  test("an operator image is versioned, and falls back to the initial when it fails", () => {
    const spec = operatorAvatarSpec("v1", "marcus");
    expect(spec).toEqual({ kind: "image", src: "/api/profile/avatar?v=v1", fallback: "M" });
    const { container } = render(<Avatar spec={spec} />);
    const img = container.querySelector("img")!;
    expect(img.getAttribute("alt")).toBe("");
    fireEvent.error(img);
    expect(disc(container).dataset.kind).toBe("accent");
    expect(disc(container).textContent).toBe("M");
  });

  test("visitor, unknown and agent discs, all hidden from assistive tech", () => {
    for (const [spec, kind, text] of [
      [{ kind: "who", name: "tobias" }, "who", "T"],
      [{ kind: "unknown" }, "neutral", "?"],
      [{ kind: "agent" }, "agent", "🤖"],
    ] as const) {
      const { container, unmount } = render(<Avatar spec={spec} />);
      expect(disc(container).dataset.kind).toBe(kind);
      expect(disc(container).textContent).toBe(text);
      expect(disc(container).getAttribute("aria-hidden")).toBe("true");
      unmount();
    }
  });
});

describe("presence words (#1931)", () => {
  test("labels and the operator's tooltip", () => {
    expect(presentLabel(1)).toBe("1 connected");
    expect(presentChip(3)).toBe("3 CONNECTED");
    expect(presenceTitle({ visitors: [{ name: "Jana" }, { name: null }] })).toBe(
      "Connected now (seen in the last 30 s):\nYou\nJana · via public link\nunnamed visitor · via public link",
    );
  });
});
