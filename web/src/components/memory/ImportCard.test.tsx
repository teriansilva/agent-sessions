import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ImportCard } from "./ImportCard";
import { resultLine } from "./importResult";
import { api, ApiError } from "../../lib/api";
import type { ImportPreview, ImportResult } from "../../types/memory";

vi.mock("../../lib/api", async importOriginal => {
  const real = await importOriginal<typeof import("../../lib/api")>();
  return { ...real, api: { memoryImportPreview: vi.fn(), importMemory: vi.fn() } };
});

const src = (over: Partial<ImportPreview["sources"][number]> = {}) => ({
  id: "s1", engine: "claude", label: "Claude Code", path: "~/.claude/projects/-w-repo/memory",
  relation: "this folder" as const, files: 201, over_agent_size: 25, over_cap: 0, beyond_max_files: 0,
  state: "ok" as const, reason: "", ...over,
});
const preview = (over: Partial<ImportPreview> = {}): ImportPreview => ({
  sources: [
    src(),
    src({ id: "s2", relation: "a subfolder session", path: "~/.claude/projects/-w-repo-web/memory", files: 2, over_agent_size: 0 }),
    src({ id: "s3", relation: "", path: "~/.claude/projects/-w-repo-old/memory", files: 0, state: "unavailable", reason: "not all of its sessions belong to this project" }),
  ],
  last: null,
  held: [],
  ...over,
});
const totals = (t: Partial<ImportResult["totals"]>): ImportResult["totals"] => ({
  imported: 0, updated: 0, unchanged: 0, kept_edits: 0, skipped_deleted: 0, held: 0, refused: 0, left_out: 0, ...t,
});
const result: ImportResult = {
  at: Date.now() / 1000,
  sources: [{ id: "s1", label: "Claude Code", path: "~", relation: "this folder", state: "ok", files: 201, imported: 156, held: 43, refused: 2 }],
  totals: totals({ imported: 156, held: 43, refused: 2 }),
  unavailable: 0,
};

describe("ImportCard (#1471 P2)", () => {
  beforeEach(() => vi.clearAllMocks());

  test("says what was found, chooses THIS folder, and never offers what can't be imported", async () => {
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview());
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    expect(await screen.findByText(/keeps/)).toHaveTextContent("Claude Code keeps 203 notes in 2 places for this project's folders. 1 place couldn't be read.");
    await userEvent.click(screen.getByRole("button", { name: "Import existing memory…" }));
    const dialog = screen.getByRole("dialog");
    const boxes = within(dialog).getAllByRole("checkbox");
    expect(boxes.map(b => [(b as HTMLInputElement).checked, (b as HTMLInputElement).disabled])).toEqual([
      [true, false], [false, false], [false, true],
    ]);
    expect(dialog).toHaveTextContent("not all of its sessions belong to this project");
    expect(dialog).toHaveTextContent("25 longer than an agent note (fine up to 32 KiB)");
    expect(within(dialog).getByRole("button", { name: "Import 201 notes" })).toBeEnabled();
    await userEvent.click(boxes[1]);
    expect(within(dialog).getByRole("button", { name: "Import 203 notes" })).toBeEnabled();
  });

  test("imports the CHOSEN ids, reports every outcome, and refreshes the project it was about", async () => {
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview());
    vi.mocked(api.importMemory).mockResolvedValue(result);
    const onImported = vi.fn(async () => {});
    render(<ImportCard pid="p-a" projectName="repo" onImported={onImported} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    await userEvent.click(screen.getByRole("button", { name: "Import 201 notes" }));
    expect(api.importMemory).toHaveBeenCalledWith("p-a", ["s1"]);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "201 notes from 1 place: 156 taken in, 43 held back, 2 refused. 45 files are held back — fix them in Claude Code's memory, then import again.",
    );
    expect(onImported).toHaveBeenCalledWith("p-a");
    expect(api.memoryImportPreview).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("held-back imports are a list to fix at the source — never a tray with a release", async () => {
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({
      last: result,
      held: [
        { source: "~", file: "deploy.md", reason: "looks like a credential", refused: false, at: 0 },
        { source: "~", file: "huge.md", reason: "larger than 32 KiB", refused: true, at: 0 },
      ],
    }));
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    expect(await screen.findByText("Held back from import · 2")).toBeInTheDocument();
    const card = screen.getByRole("region", { name: "Existing memory" });
    expect(card).toHaveTextContent("Fix them in Claude Code's memory, then import again.");
    expect(card).toHaveTextContent("refused: larger than 32 KiB");
    expect(within(card).queryByRole("button", { name: /Rewrite|Take in|Release/ })).toBeNull();
    expect(within(card).getByRole("button", { name: "Import again…" })).toBeInTheDocument();
    expect(card).toHaveTextContent(/Last import .* — 201 notes from 1 place: 156 taken in/);
  });

  test("a REPEAT import whose held file is unchanged still says it is held back", async () => {
    const held = [{ source: "~", file: "creds.md", reason: "looks like a credential", refused: false, at: 0 }];
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({ held }));
    vi.mocked(api.importMemory).mockResolvedValue({
      at: 0,
      sources: [{ id: "s1", label: "Claude Code", path: "~", relation: "this folder", state: "ok", files: 2, unchanged: 2 }],
      totals: totals({ unchanged: 2 }),
      unavailable: 0,
    });
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    await userEvent.click(screen.getByRole("button", { name: "Import 201 notes" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "2 notes from 1 place: 2 unchanged. 1 file is held back — fix it in Claude Code's memory, then import again.",
    );
  });

  test("a file held from source A is never blamed on an import of a clean source B — now or on a revisit", async () => {
    const sources = [src({ id: "a", path: "~/a/memory" }), src({ id: "b", path: "~/b/memory", relation: "a subfolder session" })];
    const heldA = [{ source: "~/a/memory", file: "creds.md", reason: "looks like a credential", refused: false, at: 0 }];
    const ranB: ImportResult = {
      at: 0,
      sources: [{ id: "b", label: "Claude Code", path: "~/b/memory", relation: "a subfolder session", state: "ok", files: 1, imported: 1 }],
      totals: totals({ imported: 1 }),
      unavailable: 0,
    };
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({ sources, held: heldA }));
    vi.mocked(api.importMemory).mockResolvedValue(ranB);
    const { unmount } = render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    const boxes = within(screen.getByRole("dialog")).getAllByRole("checkbox");
    await userEvent.click(boxes[0]); // A off
    await userEvent.click(boxes[1]); // B on
    await userEvent.click(screen.getByRole("button", { name: "Import 201 notes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("1 note from 1 place: 1 taken in.");
    expect(screen.getByRole("status")).not.toHaveTextContent("held back");
    expect(screen.getByText("Held back from import · 1")).toBeInTheDocument(); // A's file is still listed
    unmount();
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({ sources, held: heldA, last: ranB }));
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    expect(await screen.findByText(/Last import/)).not.toHaveTextContent("held back —");
  });

  test("more places than one import takes: 16 chosen, the rest wait for a second run", async () => {
    const many = Array.from({ length: 20 }, (_, i) => src({ id: `s${i}`, path: `~/.claude/projects/-w-${i}/memory`, files: 1, over_agent_size: 0 }));
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({ sources: many }));
    vi.mocked(api.importMemory).mockResolvedValue(result);
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    const dialog = screen.getByRole("dialog");
    const boxes = within(dialog).getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.filter(b => b.checked)).toHaveLength(16);
    expect(boxes.filter(b => !b.checked).every(b => b.disabled)).toBe(true);
    expect(within(dialog).getByRole("note")).toHaveTextContent("at most 16 places");
    await userEvent.click(boxes[0]); // unchoose one: another may now be chosen
    expect(boxes[19]).toBeEnabled();
    await userEvent.click(boxes[19]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Import 16 notes" }));
    expect(vi.mocked(api.importMemory).mock.calls[0][1]).toHaveLength(16);
  });

  test("nothing found says so and offers nothing to import", async () => {
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview({ sources: [] }));
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    expect(await screen.findByText("No agent memory found for this project's folders.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Import/ })).toBeNull();
  });

  test("a refused import keeps the dialog open with the reason", async () => {
    vi.mocked(api.memoryImportPreview).mockResolvedValue(preview());
    vi.mocked(api.importMemory).mockRejectedValue(new ApiError(409, "a source is no longer there — open the list again"));
    render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    await userEvent.click(screen.getByRole("button", { name: "Import 201 notes" }));
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("a source is no longer there");
  });

  test("a preview for A that settles after the SAME card moved to B never shows under B", async () => {
    let settleA!: (p: ImportPreview) => void;
    vi.mocked(api.memoryImportPreview).mockImplementation(id =>
      id === "p-a" ? new Promise(r => { settleA = r; }) : Promise.resolve(preview({ sources: [src({ files: 7 })] })),
    );
    const { rerender } = render(<ImportCard pid="p-a" projectName="alpha" onImported={async () => {}} />);
    rerender(<ImportCard pid="p-b" projectName="beta" onImported={async () => {}} />);
    expect(await screen.findByText(/keeps/)).toHaveTextContent("Claude Code keeps 7 notes in 1 place");
    await act(async () => settleA(preview({ sources: [src({ files: 999 })] })));
    expect(screen.getByText(/keeps/)).toHaveTextContent("7 notes");
  });

  test("an import for A that settles after the card moved to B refreshes A and says nothing under B", async () => {
    let settle!: (r: ImportResult) => void;
    vi.mocked(api.memoryImportPreview).mockImplementation(async id => preview({ sources: [src({ files: id === "p-a" ? 201 : 7 })] }));
    vi.mocked(api.importMemory).mockReturnValue(new Promise(r => { settle = r; }));
    const onImported = vi.fn(async () => {});
    const { rerender } = render(<ImportCard pid="p-a" projectName="alpha" onImported={onImported} />);
    await userEvent.click(await screen.findByRole("button", { name: "Import existing memory…" }));
    await userEvent.click(screen.getByRole("button", { name: "Import 201 notes" }));
    rerender(<ImportCard pid="p-b" projectName="beta" onImported={onImported} />);
    await act(async () => settle(result));
    expect(onImported).toHaveBeenCalledWith("p-a");
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByText(/keeps/)).toHaveTextContent("7 notes");
  });

  test("a preview that settles after the card is gone changes nothing", async () => {
    let settle!: (p: ImportPreview) => void;
    vi.mocked(api.memoryImportPreview).mockReturnValue(new Promise(r => { settle = r; }));
    const { unmount } = render(<ImportCard pid="p-a" projectName="repo" onImported={async () => {}} />);
    unmount();
    await act(async () => settle(preview()));
    await waitFor(() => expect(api.memoryImportPreview).toHaveBeenCalledTimes(1));
  });
});

describe("resultLine", () => {
  test("names every outcome and the places that couldn't be read", () => {
    expect(resultLine({
      at: 0,
      sources: [
        { id: "a", label: "Claude Code", path: "", relation: "this folder", state: "ok", files: 10 },
        { id: "b", label: "Claude Code", path: "", relation: "", state: "unavailable", reason: "writable by other users" },
      ],
      totals: totals({ imported: 3, updated: 1, kept_edits: 1, skipped_deleted: 1, unchanged: 2, left_out: 2 }),
      unavailable: 1,
    })).toBe(
      "10 notes from 1 place: 3 taken in, 1 updated, 1 kept your edits, 2 left out — memory is full, 1 you deleted stay deleted, 2 unchanged. 1 place couldn't be read (writable by other users).",
    );
  });
});
