/** What's new on the Updates card (#1085): bundled notes, and a GitHub link only on stable. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { releaseNotesUrl } from "../../lib/links";
import type { WhatsNewRelease } from "../../whatsnew/releases";

import { UpdateWhatsNew } from "./UpdateWhatsNew";

const RELEASES: WhatsNewRelease[] = [
  {
    version: "0.21.0",
    assetDir: "x",
    slides: [
      {
        id: "intro",
        eyebrow: "Version 0.21",
        title: "Ask answers about missions.",
        body: "b",
        tiles: [{ label: "Ask", text: "missions too", slide: "ask" }],
      },
    ],
  },
];

test("stable links the AVAILABLE release's notes when an update is offered", async () => {
  const onOpen = vi.fn();
  render(
    <UpdateWhatsNew
      channel="stable"
      current="0.20.3"
      available="v0.21.0"
      onOpen={onOpen}
      releases={RELEASES}
    />,
  );
  expect(screen.getByRole("heading", { name: "What's new in 0.21" })).toBeInTheDocument();
  expect(screen.getByText("Ask answers about missions.")).toBeInTheDocument();
  const link = screen.getByTestId("update-release-notes");
  expect(link).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v0.21.0",
  );
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await userEvent.click(screen.getByRole("button", { name: /show what's new/i }));
  expect(onOpen).toHaveBeenCalled();
});

test("stable with no update links the installed release", () => {
  render(<UpdateWhatsNew channel="stable" current="0.20.3" available={null} releases={RELEASES} />);
  expect(screen.getByTestId("update-release-notes")).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/releases/tag/v0.20.3",
  );
});

test("main has no release, so no link — it says why", () => {
  render(
    <UpdateWhatsNew channel="main" current="0.20.3" available="64f301f" releases={RELEASES} />,
  );
  expect(screen.queryByTestId("update-release-notes")).toBeNull();
  expect(screen.getByTestId("update-main-notes")).toHaveTextContent("development branch");
});

test.each([
  ["v1.2.3", "https://github.com/teriansilva/agent-sessions/releases/tag/v1.2.3"],
  ["1.2.3", "https://github.com/teriansilva/agent-sessions/releases/tag/v1.2.3"],
  ["64f301f", null],
  ["v1.2.3/../../evil", null],
  ["dev", null],
  [null, null],
])("releaseNotesUrl(%s) → %s", (v, want) => {
  expect(releaseNotesUrl(v)).toBe(want);
});
