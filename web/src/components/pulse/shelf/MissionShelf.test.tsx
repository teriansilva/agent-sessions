/** The shelf view and its lightbox (#1740 P1a), through the real components with the API mocked:
 *  every deliverable kind renders as its typed viewer and agent text stays text; verdicts are made
 *  against the revision read, and a moved shelf is re-read before going again — never over a
 *  verdict someone else set. Real-browser behaviour (layout, focus geometry, media playback) is
 *  `e2e/mission-shelf.spec.ts`. */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../../lib/api";
import type { ShelfItem } from "../../../types/shelf";
import { MissionShelf } from "./MissionShelf";
import type { MissionShelf as ShelfData, ShelfState } from "./useMissionShelf";

vi.mock("../../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../../lib/api")>("../../../lib/api");
  return {
    ...actual,
    api: {
      setShelfVerdict: vi.fn(),
      missionShelfBlob: vi.fn(() => new Promise(() => {})),
      missionShelfProbe: vi.fn(),
    },
  };
});

const M = "msn_" + "1".repeat(32);
const S1 = "claude:aaaaaaaa-0000-4000-8000-000000000001";

function it_(over: Partial<ShelfItem>): ShelfItem {
  return {
    id: "shf_" + String(over.seq ?? 1).padStart(32, "0"),
    seq: 1,
    source: "delivered",
    kind: "text",
    title: "Item",
    why: "",
    session_key: S1,
    ref_type: "text",
    size: 10,
    sha256: null,
    created_at: 1_700_000_000,
    verdict: null,
    note: "",
    verdict_at: null,
    ...over,
  };
}

const ITEMS: ShelfItem[] = [
  it_({ seq: 1, kind: "image", title: "Poster", ref_type: "path", name: "poster.png", path: "/p/poster.png", mime: "image/png", viewable: true, file: { state: "ok" }, sha256: "a".repeat(64) }),
  it_({ seq: 2, kind: "document", title: "Report", ref_type: "path", name: "report.pdf", path: "/p/report.pdf", mime: null, viewable: false, file: { state: "ok" } }),
  it_({ seq: 3, kind: "link", title: "Pull request", ref_type: "url", url: "https://git.example.org/a/pulls/4", host: "git.example.org", url_path: "/a/pulls/4", size: null }),
  it_({ seq: 4, kind: "text", title: "Finding", ref_type: "text", text: "<b>not bold</b>\nsecond line", why: "worth <i>a</i> look" }),
  it_({ seq: 5, kind: "image", title: "Gone shot", ref_type: "path", name: "gone.png", path: "/p/gone.png", mime: "image/png", file: { state: "missing" } }),
  it_({ seq: 6, kind: "image", title: "Changed shot", ref_type: "path", name: "c.png", path: "/p/c.png", mime: "image/png", sha256: "b".repeat(64), file: { state: "changed", disk_sha256: "c".repeat(64), disk_mtime: 1_700_000_100 } }),
];

function state(items = ITEMS, revision = 6): ShelfState {
  return {
    revision,
    items,
    counts: {
      total: items.length,
      by_kind: { image: 3, video: 0, audio: 0, document: 1, code: 0, link: 1, text: 1 },
      by_source: { delivered: items.length, found: 0 },
      unreviewed: items.filter((i) => i.verdict === null).length,
      file_checks: { changed: 1, missing: 1, inaccessible: 0 },
    },
  };
}

function data(over: Partial<ShelfData> = {}): ShelfData {
  return {
    shelf: state(),
    error: null,
    reload: vi.fn(async () => state()),
    applyVerdict: vi.fn(),
    ...over,
  };
}

function view(d: ShelfData) {
  return render(
    <MemoryRouter>
      <MissionShelf missionId={M} data={d} sessionTitles={{ [S1]: "Render the hero video" }} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.setShelfVerdict).mockReset();
});

test("each kind in the fixture gets its tile, grouped by session, and agent text stays text", () => {
  view(data());
  const group = screen.getByRole("region", { name: "Render the hero video" });
  expect(within(group).getAllByRole("button", { name: /^(Image|Document|Link|Text): / })).toHaveLength(6);
  expect(screen.getByText("git.example.org")).toBeTruthy();
  // The text item's excerpt is the literal string — no element was made from it.
  const tile = screen.getByRole("button", { name: /^Text:\sFinding(,|$)/ });
  expect(tile.textContent).toContain("<b>not bold</b>");
  expect(tile.querySelector("b")).toBeNull();
  expect(screen.getByRole("button", { name: /^Image:\sGone\sshot(,|$)/ }).textContent).toContain("File not found");
  expect(screen.getByRole("button", { name: /^Image:\sChanged\sshot(,|$)/ }).textContent).toContain("Changed");
  expect(screen.getByText("6", { selector: "[data-shelf-unreviewed]" })).toBeTruthy();
});

test("the kind chips filter the grid and keep the whole shelf's counts", async () => {
  const user = userEvent.setup();
  view(data());
  const { container } = { container: document.body };
  await user.click(container.querySelector<HTMLElement>("[data-shelf-kind-chip='link']")!);
  expect(screen.getAllByRole("button", { name: /^(Image|Document|Link|Text): / })).toHaveLength(1);
  const all = container.querySelector<HTMLElement>("[data-shelf-kind-chip='all']")!;
  expect(all.getAttribute("aria-pressed")).toBe("false");
  expect(all.textContent).toContain("6");
});

test("a link opens inert in a new tab; a PDF is download-only; text shows in full", async () => {
  const user = userEvent.setup();
  view(data());
  await user.click(screen.getByRole("button", { name: /^Link:\sPull\srequest(,|$)/ }));
  let dialog = screen.getByRole("dialog", { name: "Pull request" });
  const link = within(dialog).getAllByRole("link").find((a) => a.hasAttribute("data-shelf-link"))!;
  expect(link.getAttribute("href")).toBe("https://git.example.org/a/pulls/4");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  await user.click(within(dialog).getAllByRole("button", { name: "Previous item" })[0]);
  dialog = screen.getByRole("dialog", { name: "Report" });
  expect(dialog.querySelector("[data-shelf-download-only]")).not.toBeNull();
  const dl = dialog.querySelector<HTMLAnchorElement>("[data-shelf-download]")!;
  expect(dl.getAttribute("href")).toBe(`/api/missions/${M}/shelf/${ITEMS[1].id}/raw?download=1`);
  await user.keyboard("{ArrowRight}{ArrowRight}");
  dialog = screen.getByRole("dialog", { name: "Finding" });
  const full = dialog.querySelector("[data-shelf-text-item]")!;
  expect(full.textContent).toBe("<b>not bold</b>\nsecond line");
  expect(full.querySelector("b")).toBeNull();
});

test("a changed file shows both hashes; a missing one never asks for bytes", async () => {
  const user = userEvent.setup();
  view(data());
  await user.click(screen.getByRole("button", { name: /^Image:\sChanged\sshot(,|$)/ }));
  const dialog = screen.getByRole("dialog", { name: "Changed shot" });
  expect(dialog.textContent).toContain("Changed since delivered");
  expect(dialog.textContent).toContain("bbbb…bbbb");
  expect(dialog.textContent).toContain("cccc…cccc");
  await user.click(within(dialog).getAllByRole("button", { name: "Previous item" })[0]);
  const gone = screen.getByRole("dialog", { name: "Gone shot" });
  expect(gone.querySelector("[data-shelf-gone='missing']")).not.toBeNull();
  expect(gone.querySelector("img, video, audio")).toBeNull();
  // Never fetched at all (not merely fetched and not shown).
  expect(vi.mocked(api.missionShelfBlob).mock.calls.map((c) => c[1])).not.toContain(ITEMS[4].id);
});

test("a verdict is made against the revision read and folded in place", async () => {
  const user = userEvent.setup();
  const d = data();
  const saved = { ...ITEMS[0], verdict: "needs_work" as const, note: "re-export", verdict_at: 1_700_000_200 };
  vi.mocked(api.setShelfVerdict).mockResolvedValue({ revision: 7, item: saved });
  view(d);
  await user.click(screen.getByRole("button", { name: /^Image:\sPoster(,|$)/ }));
  const dialog = screen.getByRole("dialog", { name: "Poster" });
  await user.type(within(dialog).getByLabelText("Note · optional"), "re-export");
  await user.click(within(dialog).getAllByRole("radio", { name: "Needs work" })[0]);
  expect(api.setShelfVerdict).toHaveBeenCalledWith(M, ITEMS[0].id, {
    expected_revision: 6,
    verdict: "needs_work",
    note: "re-export",
  });
  expect(d.applyVerdict).toHaveBeenCalledWith(7, saved);
});

test("a moved shelf is re-read, and a verdict changed elsewhere is never overwritten", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setShelfVerdict).mockRejectedValueOnce(new ApiError(409, "moved", { revision: 9 }));
  const someoneElse = state(ITEMS.map((i, n) => (n === 0 ? { ...i, verdict: "good" as const } : i)), 9);
  const d = data({ reload: vi.fn(async () => someoneElse) });
  view(d);
  await user.click(screen.getByRole("button", { name: /^Image:\sPoster(,|$)/ }));
  const dialog = screen.getByRole("dialog", { name: "Poster" });
  await user.click(within(dialog).getAllByRole("radio", { name: "Needs work" })[0]);
  expect(api.setShelfVerdict).toHaveBeenCalledTimes(1);
  expect(within(dialog).getByRole("alert").textContent).toContain("changed elsewhere");
});

test("a moved shelf whose item is unchanged goes again at the new revision", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setShelfVerdict)
    .mockRejectedValueOnce(new ApiError(409, "moved", { revision: 9 }))
    .mockResolvedValueOnce({ revision: 10, item: { ...ITEMS[0], verdict: "good" } });
  const d = data({ reload: vi.fn(async () => state(ITEMS, 9)) });
  view(d);
  await user.click(screen.getByRole("button", { name: /^Image:\sPoster(,|$)/ }));
  await user.click(within(screen.getByRole("dialog")).getAllByRole("radio", { name: "Looks good" })[0]);
  expect(vi.mocked(api.setShelfVerdict).mock.calls.map((c) => c[2].expected_revision)).toEqual([6, 9]);
  expect(d.applyVerdict).toHaveBeenCalledWith(10, expect.objectContaining({ verdict: "good" }));
});

test("Escape closes the lightbox and focus goes back to the tile", async () => {
  const user = userEvent.setup();
  view(data());
  const tile = screen.getByRole("button", { name: /^Text:\sFinding(,|$)/ });
  await user.click(tile);
  expect(document.activeElement?.getAttribute("aria-label")).toBe("Close");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => {
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  });
  expect(document.activeElement).toBe(tile);
});

test("an empty shelf says what will arrive", () => {
  view(data({ shelf: { ...state([]), counts: { ...state([]).counts, total: 0 } } }));
  expect(screen.getByText("Nothing on the shelf yet")).toBeTruthy();
});

test("a verdict pressed while the note's blur save is in flight is queued, never swallowed", async () => {
  const user = userEvent.setup();
  const verdicted = ITEMS.map((i, n) => (n === 0 ? { ...i, verdict: "good" as const } : i));
  let settle!: (v: unknown) => void;
  vi.mocked(api.setShelfVerdict)
    .mockImplementationOnce(() => new Promise((res) => (settle = res)) as never)
    .mockResolvedValueOnce({ revision: 8, item: { ...verdicted[0], verdict: "needs_work", note: "x" } });
  view(data({ shelf: state(verdicted) }));
  await user.click(screen.getByRole("button", { name: /^Image: Poster/ }));
  const dialog = screen.getByRole("dialog", { name: "Poster" });
  await user.type(within(dialog).getByLabelText("Note · optional"), "x");
  // The click blurs the note first (a note-only save starts), then lands on the other verdict.
  await user.click(within(dialog).getAllByRole("radio", { name: "Needs work" })[0]);
  expect(vi.mocked(api.setShelfVerdict).mock.calls.map((c) => c[2].verdict)).toEqual(["good"]);
  await act(async () => {
    settle({ revision: 7, item: { ...verdicted[0], note: "x" } });
  });
  expect(vi.mocked(api.setShelfVerdict).mock.calls.map((c) => [c[2].verdict, c[2].note])).toEqual([
    ["good", "x"],
    ["needs_work", "x"],
  ]);
});

test("an answer for an item the operator moved away from never paints over the next one", async () => {
  const user = userEvent.setup();
  let settle!: (v: unknown) => void;
  vi.mocked(api.setShelfVerdict).mockImplementationOnce(
    () => new Promise((_res, rej) => (settle = () => rej(new ApiError(500, "boom")))) as never,
  );
  view(data());
  await user.click(screen.getByRole("button", { name: /^Image: Poster/ }));
  await user.click(within(screen.getByRole("dialog")).getAllByRole("radio", { name: "Looks good" })[0]);
  await user.keyboard("{ArrowRight}");
  expect(screen.getByRole("dialog", { name: "Report" })).toBeTruthy();
  await act(async () => {
    settle(null);
  });
  expect(within(screen.getByRole("dialog", { name: "Report" })).queryByRole("alert")).toBeNull();
});
