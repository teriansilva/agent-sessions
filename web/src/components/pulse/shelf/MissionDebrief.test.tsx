/** The mission debrief (#1740 P2) through the real component with the API mocked: the summary
 *  beside the records, the out-of-date marker with the current facts, the structured debrief with
 *  no endpoint (every objective and every shelf group named), the three still-open lists kept apart
 *  with "None" when empty, Regenerate and its reason, and the one-line collapse once read.
 *  Real-browser behaviour is `e2e/mission-shelf.spec.ts`. */
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../../../lib/api";
import type { Debrief, DebriefFacts } from "../../../types/shelf";
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
      missionShelfDiff: vi.fn(),
      missionDebrief: vi.fn(),
      regenerateDebrief: vi.fn(),
    },
  };
});

const M = "msn_" + "2".repeat(32);
const S1 = "claude:aaaaaaaa-0000-4000-8000-000000000001";
const S2 = "codex:bbbbbbbb-0000-7000-8000-000000000002";
const QUOTE = "Rendered out/hero-v021.webm — 1920×1080, 00:31, 24 fps.";

function counts(): ShelfState["counts"] {
  return {
    total: 13,
    by_kind: { image: 4, video: 1, audio: 1, document: 1, code: 2, link: 2, text: 2 },
    by_source: { delivered: 10, found: 3 },
    unreviewed: 9,
    file_checks: { changed: 1, missing: 1, inaccessible: 0 },
  };
}

function facts(over: Partial<DebriefFacts> = {}): DebriefFacts {
  return {
    mission_id: M,
    title: "Launch video for v0.21",
    state: "review",
    asked: "Produce the launch video package for v0.21",
    sessions: [
      { key: S1, role: "primary", added_at: 1_700_000_000, removed_at: 1_700_003_000 },
      { key: S2, role: "primary", added_at: 1_700_000_000, removed_at: null },
    ],
    objectives: [
      {
        key: "o1",
        title: "A 30-second hero video is rendered at 1080p",
        state: "met",
        met: true,
        judged: {
          met: true,
          confidence: 0.96,
          evidence: [{ source: `transcript:${S1}`, quote: QUOTE }],
          checked_at: 1_700_000_100,
        },
      },
      {
        key: "o2",
        title: "Voice-over matches the approved script",
        state: "pending",
        met: false,
        judged: { met: false, confidence: 0.64, evidence: [], checked_at: 1_700_000_200 },
      },
    ],
    shelf: {
      counts: counts(),
      groups: [
        { session_key: S1, total: 5, by_kind: { video: 1 } },
        { session_key: S2, total: 8, by_kind: { image: 4 } },
      ],
    },
    open: {
      objectives: [
        {
          key: "o2",
          title: "Voice-over matches the approved script",
          state: "pending",
          judged: { met: false, confidence: 0.64, evidence: [], checked_at: 1 },
        },
      ],
      objectives_total: 1,
      needs_work: [
        {
          id: "shf_" + "a".repeat(32),
          kind: "audio",
          source: "delivered",
          title: "Voice-over, take 3",
          session_key: S1,
          note: "Keep the full second paragraph",
        },
      ],
      needs_work_total: 1,
      found_not_delivered: [],
      found_not_delivered_total: 0,
    },
    integrity: { changed: [], missing: [] },
    revision: "rev-a",
    ...over,
  };
}

function debrief(over: Partial<Debrief> = {}): Debrief {
  return {
    mission_id: M,
    state: "review",
    configured: true,
    facts: facts(),
    summary: {
      text: "Two sessions worked. Claude rendered the hero video.",
      revision: "rev-a",
      written_at: 1_700_004_000,
    },
    fresh: true,
    generating: false,
    error: null,
    ...over,
  };
}

function shelfData(): ShelfData {
  const st: ShelfState = { revision: 6, items: [], foundPending: [], counts: counts() };
  return { shelf: st, error: null, reload: vi.fn(async () => st), applyVerdict: vi.fn() };
}

function view(state = "review", configured = true) {
  return render(
    <MemoryRouter>
      <MissionShelf
        missionId={M}
        data={shelfData()}
        sessionTitles={{ [S1]: "Render the hero video" }}
        missionState={state}
        configured={configured}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(api.missionDebrief).mockReset();
  vi.mocked(api.regenerateDebrief).mockReset();
});
afterEach(() => vi.useRealTimers());

async function shown() {
  return await screen.findByTestId("mission-debrief", undefined, { timeout: 3000 }).then(
    async (el) => {
      await within(el).findByText("What was asked");
      return el;
    },
  );
}

test("a mission in review opens on its debrief: the summary beside the records", async () => {
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief());
  view();
  const d = await shown();
  expect(within(d).getByText("AI summary · from the records")).toBeTruthy();
  expect(d.querySelector("[data-debrief-asked]")?.textContent).toBe(
    "Produce the launch video package for v0.21",
  );
  expect(d.querySelector("[data-debrief-summary]")?.textContent).toContain(
    "Claude rendered the hero video",
  );
  // Objectives with the judge's own words.
  const objs = d.querySelector("[data-debrief-objectives]") as HTMLElement;
  expect(within(objs).getByText(`“${QUOTE}”`)).toBeTruthy();
  expect(within(objs).getByText("judged 0.96")).toBeTruthy();
  // Three separate lists, each with its count, "None" when empty.
  const lists = [...d.querySelectorAll<HTMLElement>("[data-debrief-open]")];
  expect(lists.map((l) => l.dataset.debriefOpen)).toEqual(["objectives", "needs_work", "found"]);
  expect(lists.map((l) => l.querySelector("[data-debrief-open-count]")?.textContent)).toEqual([
    "1",
    "1",
    "0",
  ]);
  expect(within(lists[1]).getByText("“Keep the full second paragraph”", { exact: false })).toBeTruthy();
  expect(within(lists[2]).getByText("None")).toBeTruthy();
  expect(within(lists[2]).getByText("Provenance, not necessarily unfinished work.")).toBeTruthy();
  // File checks sit under the shelf counts, never as a still-open row.
  const checks = d.querySelector("[data-debrief-file-checks]") as HTMLElement;
  expect(checks.textContent).toContain("1 changed since delivered");
  expect(lists.some((l) => l.textContent?.includes("changed since delivered"))).toBe(false);
  expect(d.hasAttribute("data-fresh")).toBe(true);
  expect(d.querySelector("[data-debrief-stale]")).toBeNull();
});

test("a running mission shows no debrief and never asks for one", async () => {
  view("running");
  await screen.findByTestId("mission-shelf");
  expect(screen.queryByTestId("mission-debrief")).toBeNull();
  expect(api.missionDebrief).not.toHaveBeenCalled();
});

test("a summary the records have moved past is marked out of date, beside the current facts", async () => {
  vi.mocked(api.missionDebrief).mockResolvedValue(
    debrief({
      fresh: false,
      facts: facts({ revision: "rev-b", open: { ...facts().open, objectives: [], objectives_total: 0 } }),
    }),
  );
  view();
  const d = await shown();
  expect(d.hasAttribute("data-fresh")).toBe(false);
  expect(d.querySelector("[data-debrief-stale]")?.textContent).toContain(
    "the records changed after this summary was written",
  );
  // The lists are the current records, not the summary's.
  const first = d.querySelector('[data-debrief-open="objectives"]') as HTMLElement;
  expect(first.querySelector("[data-debrief-open-count]")?.textContent).toBe("0");
  expect(within(first).getByText("None")).toBeTruthy();
});

test("with no endpoint the debrief is the records: every objective and every shelf group named", async () => {
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief({ configured: false, summary: null }));
  view("review", false);
  const d = await shown();
  expect(d.dataset.mode).toBe("records");
  expect(d.querySelector("[data-debrief-no-ai]")?.textContent).toContain(
    "No AI endpoint is configured",
  );
  expect(d.querySelector("[data-debrief-regenerate]")).toBeNull();
  for (const o of facts().objectives) expect(within(d).getAllByText(o.title).length).toBeGreaterThan(0);
  const groups = [...d.querySelectorAll<HTMLElement>("[data-debrief-group]")].map(
    (g) => g.dataset.debriefGroup,
  );
  expect(groups).toEqual([S1, S2]);
  expect(d.querySelector("[data-debrief-sessions]")?.textContent).toContain("Render the hero video");
});

test("Regenerate writes from the records now, and says why when it could not", async () => {
  const user = userEvent.setup();
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief({ fresh: false }));
  vi.mocked(api.regenerateDebrief)
    .mockRejectedValueOnce(
      new ApiError(502, "the summary was discarded: the summary quoted something the mission's records do not hold"),
    )
    .mockResolvedValueOnce(
      debrief({ summary: { text: "Rewritten from the records.", revision: "rev-a", written_at: 1_700_005_000 } }),
    );
  view();
  const d = await shown();
  await user.click(within(d).getByRole("button", { name: /Regenerate/ }));
  expect((await within(d).findByRole("alert")).textContent).toContain("quoted something");
  await user.click(within(d).getByRole("button", { name: /Regenerate/ }));
  expect((await within(d).findByText("Rewritten from the records.")).hasAttribute("data-debrief-summary")).toBe(
    true,
  );
  expect(within(d).queryByRole("alert")).toBeNull();
  expect(api.regenerateDebrief).toHaveBeenCalledWith(M);
});

test("once read it collapses to one line, and new records open it again", async () => {
  const user = userEvent.setup();
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief());
  const { unmount } = view();
  const d = await shown();
  await user.click(within(d).getByRole("button", { name: /Collapse/ }));
  const line = await screen.findByText(/objectives met/);
  expect(line.textContent).toBe("1 of 2 objectives met · 13 on the shelf · 9 to review · 2 still open");
  unmount();
  // Same records: it stays read.
  view();
  expect((await screen.findByTestId("mission-debrief")).hasAttribute("data-collapsed")).toBe(true);
  // New records: open again.
  await act(async () => {});
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief({ facts: facts({ revision: "rev-c" }), fresh: false }));
  render(
    <MemoryRouter>
      <MissionShelf missionId={M} data={shelfData()} sessionTitles={{}} missionState="done" configured />
    </MemoryRouter>,
  );
  const all = await screen.findAllByTestId("mission-debrief");
  await within(all[all.length - 1]).findByText("What was asked");
  expect(all[all.length - 1].hasAttribute("data-collapsed")).toBe(false);
});

test("Review N items shows the unreviewed items and marks the debrief read", async () => {
  const user = userEvent.setup();
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief());
  view();
  const d = await shown();
  await user.click(within(d).getByRole("button", { name: "Review 9 items" }));
  expect(screen.getByTestId("mission-debrief").hasAttribute("data-collapsed")).toBe(true);
  expect(
    screen.getByRole("button", { name: /Unreviewed only/ }).getAttribute("aria-pressed"),
  ).toBe("true");
});

function shelfWith(fileState: "ok" | "changed"): ShelfData {
  const item = {
    id: "shf_" + "b".repeat(32),
    seq: 1,
    source: "delivered" as const,
    kind: "image" as const,
    title: "Poster frame",
    why: "",
    session_key: S1,
    ref_type: "path" as const,
    name: "poster.png",
    path: "/p/poster.png",
    mime: "image/png",
    viewable: true,
    size: 10,
    sha256: "a".repeat(64),
    created_at: 1_700_000_000,
    verdict: null,
    note: "",
    verdict_at: null,
    file: { state: fileState },
  };
  // The SAME revision: a file changing on disk never moves it.
  const st: ShelfState = { revision: 6, items: [item as never], foundPending: [], counts: counts() };
  return { shelf: st, error: null, reload: vi.fn(async () => st), applyVerdict: vi.fn() };
}

test("a file changing on disk is a reason to read the debrief again, though the shelf revision did not move", async () => {
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief());
  const el = (d: ShelfData) => (
    <MemoryRouter>
      <MissionShelf missionId={M} data={d} sessionTitles={{}} missionState="review" configured />
    </MemoryRouter>
  );
  const { rerender } = render(el(shelfWith("ok")));
  await shown();
  expect(api.missionDebrief).toHaveBeenCalledTimes(1);
  vi.mocked(api.missionDebrief).mockResolvedValue(
    debrief({ fresh: false, facts: facts({ revision: "rev-file" }) }),
  );
  rerender(el(shelfWith("changed")));
  expect(await screen.findByText(/the records changed after this summary was written/)).toBeTruthy();
  expect(api.missionDebrief).toHaveBeenCalledTimes(2);
});

test("new records open a collapsed debrief again while the page stays mounted", async () => {
  const user = userEvent.setup();
  vi.mocked(api.missionDebrief).mockResolvedValue(debrief());
  const el = (d: ShelfData) => (
    <MemoryRouter>
      <MissionShelf missionId={M} data={d} sessionTitles={{}} missionState="review" configured />
    </MemoryRouter>
  );
  const { rerender } = render(el(shelfWith("ok")));
  const d = await shown();
  await user.click(within(d).getByRole("button", { name: /Collapse/ }));
  expect(screen.getByTestId("mission-debrief").hasAttribute("data-collapsed")).toBe(true);
  expect(screen.getByRole("button", { name: /Expand/ }).getAttribute("aria-controls")).toBeNull();
  vi.mocked(api.missionDebrief).mockResolvedValue(
    debrief({ fresh: false, facts: facts({ revision: "rev-new" }) }),
  );
  rerender(el(shelfWith("changed")));
  await within(screen.getByTestId("mission-debrief")).findByText("What was asked");
  expect(screen.getByTestId("mission-debrief").hasAttribute("data-collapsed")).toBe(false);
});

test("a still-open list longer than the server lists says how many more there are", async () => {
  const f = facts();
  vi.mocked(api.missionDebrief).mockResolvedValue(
    debrief({ facts: { ...f, open: { ...f.open, needs_work_total: 73 } } }),
  );
  view();
  const d = await shown();
  const list = d.querySelector('[data-debrief-open="needs_work"]') as HTMLElement;
  expect(list.querySelector("[data-debrief-open-count]")?.textContent).toBe("73");
  expect(list.querySelector("[data-debrief-open-more]")?.textContent).toBe(
    "The first 1 of 73.",
  );
  expect(d.querySelector('[data-debrief-open="objectives"] [data-debrief-open-more]')).toBeNull();
});
