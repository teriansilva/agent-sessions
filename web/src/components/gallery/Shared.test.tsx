import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { GalleryIndex, GalleryOrigin } from "../../types/gallery";
import { Origin } from "./Shared";

/** `Origin` is shared by the skill and playbook detail pages (#1440). Its one job on a copy is to
 *  say where the copy came from and whether it can be updated — never to take the page down when
 *  the stored listing is not a listing (#2254). */
let catalog: GalleryIndex | null = null;
vi.mock("./useGallery", () => ({
  useGallery: () => ({
    data: catalog,
    error: null,
    loading: false,
    checking: false,
    problem: null,
    reload: vi.fn(),
    check: vi.fn(),
  }),
  galleryWait: () => "",
}));

afterEach(cleanup);

const playbookOrigin: GalleryOrigin = {
  state: "verified",
  kind: "playbook",
  id: "inbox-triage",
  version: "1.2.0",
  title: "Inbox triage",
  publisher: "BattleLab",
  entry_digest: "e".repeat(64),
  base_revision: "b".repeat(64),
};

function renderPlaybookOrigin(value: GalleryOrigin = playbookOrigin) {
  return render(
    <Origin
      value={value}
      kind="playbook"
      id="inbox-triage"
      revision={value.base_revision}
    />,
  );
}

test("a playbook copy renders the unavailable note when the listing is not a listing (#2254)", () => {
  catalog = {} as GalleryIndex;
  renderPlaybookOrigin();
  expect(
    screen.getByText("From the gallery · v1.2.0 · unchanged"),
  ).toBeVisible();
  expect(screen.getByText("Can't check for updates right now.")).toBeVisible();
  expect(screen.queryByTestId("update-available")).toBeNull();
  expect(screen.queryByRole("link", { name: /Review update/ })).toBeNull();
});

test("entries that are not an array are unreachable too (#2254)", () => {
  catalog = { state: "ready", entries: null } as unknown as GalleryIndex;
  renderPlaybookOrigin();
  expect(screen.getByText("Can't check for updates right now.")).toBeVisible();
  expect(screen.queryByTestId("update-available")).toBeNull();
});
