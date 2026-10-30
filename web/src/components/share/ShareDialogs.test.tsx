import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import { api, ApiError } from "../../lib/api";
import type { SharePreview, SharePreviewItem } from "../../types/share";
import { ShareActions } from "./ShareDialogs";
import { resetImportMemory } from "./shareRules";
import { useShareImports } from "./useShareImports";

vi.mock("../../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../lib/api")>();
  return {
    ApiError: real.ApiError,
    api: {
      shareCatalog: vi.fn(),
      shareImports: vi.fn(),
      shareExport: vi.fn(),
      sharePreview: vi.fn(),
      shareConfirm: vi.fn(),
    },
  };
});

const PREVIEW: SharePreview = {
  token: "a".repeat(32),
  expires_at: "2026-10-27T10:15:00Z",
  bundle: { digest: "9f3c" + "0".repeat(56) + "a71e", size: 4096, format: 1, created_at: "2026-10-26T14:02:00Z", app_version: "0.24.1", source: null, items: 3, files: 3 },
  items: [
    { key: "skill:a", kind: "skill", id: "a", title: "a", dest: "a-2", status: "renamed", reason: null, depends_on: [], uses: [], files: [{ path: "SKILL.md", size: 5, text: "# a\n" }] },
    { key: "skill:b", kind: "skill", id: "b", title: "b", dest: "b", status: "new", reason: null, depends_on: [], uses: [], files: [] },
    { key: "playbook:p", kind: "playbook", id: "p", title: "P", dest: "p", status: "new", reason: null, depends_on: ["skill:a", "skill:b"], uses: [{ from: "a", to: "a-2" }], files: [] },
  ],
};

beforeEach(() => {
  vi.resetAllMocks();
  resetImportMemory();
  // The library's import record, readable and empty: a first import of a file.
  vi.mocked(api.shareImports).mockResolvedValue({ imports: {} });
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});

const mount = (onImported = vi.fn()) =>
  render(
    <MemoryRouter>
      <ShareActions kind="skill" onImported={onImported} />
    </MemoryRouter>,
  );

test("export: choosing a playbook includes its skills, locked, and downloads the chosen keys", async () => {
  vi.mocked(api.shareCatalog).mockResolvedValue({
    imports: {},
    items: [
      { kind: "skill", id: "a", title: "a", depends_on: [], exportable: true, reason: null, source: "local" },
      { kind: "playbook", id: "p", title: "P", depends_on: ["skill:a"], exportable: true, reason: null, source: "local" },
    ],
  });
  vi.mocked(api.shareExport).mockResolvedValue(new Blob(["x"]));
  globalThis.URL.createObjectURL = vi.fn(() => "blob:x");
  globalThis.URL.revokeObjectURL = vi.fn();
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Export…" }));
  const dialog = await screen.findByTestId("share-export");
  fireEvent.click(await within(dialog).findByLabelText("Playbook P"));
  const skill = within(dialog).getByLabelText("Skill a (included)");
  expect(skill).toBeChecked();
  expect(skill).toBeDisabled();
  expect(within(dialog).getByText(/Included — needed by p/)).toBeInTheDocument();
  expect(screen.getByTestId("share-export-summary")).toHaveTextContent("2 items · 1 chosen · 1 included");
  fireEvent.click(within(dialog).getByRole("button", { name: /Download bundle/ }));
  await waitFor(() => expect(api.shareExport).toHaveBeenCalledWith([{ kind: "playbook", id: "p" }]));
  expect(await within(dialog).findByRole("status")).toHaveTextContent(/Downloaded battlelab-.*\.battlelab with 2 items/);
});

test("export: an empty library says so", async () => {
  vi.mocked(api.shareCatalog).mockResolvedValue({ imports: {}, items: [] });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Export…" }));
  expect(await screen.findByText("Nothing to export yet")).toBeInTheDocument();
});

async function preview() {
  vi.mocked(api.sharePreview).mockResolvedValue(PREVIEW);
  const onImported = vi.fn();
  mount(onImported);
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  const file = new File(["x"], "kit.battlelab");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [file] } });
  await within(dialog).findByText(/Not signed — imported from a file/);
  return { dialog, onImported };
}

test("import: a deselected dependency refuses its dependent before confirm, and only the selection is sent", async () => {
  const { dialog } = await preview();
  expect(within(dialog).getByTestId("share-preview-row-skill:a")).toHaveTextContent("Renamed");
  fireEvent.click(within(dialog).getByLabelText("Import Skill b"));
  const row = within(dialog).getByTestId("share-preview-row-playbook:p");
  expect(row).toHaveAttribute("data-state", "refused");
  expect(row).toHaveTextContent("Needs skill b, which isn't being imported");
  expect(screen.getByTestId("share-preview-summary")).toHaveTextContent("1 to import");
  vi.mocked(api.shareConfirm).mockResolvedValue({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: [
      { key: "skill:a", kind: "skill", id: "a", title: "a", status: "created", dest: "a-2", reason: null },
      { key: "skill:b", kind: "skill", id: "b", title: "b", status: "not_selected", dest: null, reason: null },
      { key: "playbook:p", kind: "playbook", id: "p", title: "P", status: "refused", dest: null, reason: "needs skill b, which isn't being imported" },
    ],
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 1 item" }));
  await waitFor(() => expect(api.shareConfirm).toHaveBeenCalledWith(PREVIEW.token, ["skill:a", "playbook:p"]));
  const created = await within(dialog).findByTestId("share-result-row-skill:a");
  expect(within(created).getByRole("link", { name: /Open/ })).toHaveAttribute("href", "/library/skills/a-2");
});

test("import: a stale preview asks to preview again, nothing else", async () => {
  const { dialog } = await preview();
  vi.mocked(api.shareConfirm).mockRejectedValue(new ApiError(409, "changed", { reason: "changed" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  expect(await within(dialog).findByText(/Your library changed since this preview/)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: /Preview again/ })).toBeEnabled();
});

test("import: a refused file says why and that nothing was read", async () => {
  vi.mocked(api.sharePreview).mockRejectedValue(new ApiError(422, "this bundle is format 2; this install reads format 1."));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [new File(["x"], "new.battlelab")] } });
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(/format 2.*Nothing was read/);
});

function Marks() {
  const keys = useShareImports();
  return <p data-testid="marks">{[...keys].join(",")}</p>;
}

test("imported marks are optional data: a stub answer `{}` shows none", async () => {
  vi.mocked(api.shareImports).mockResolvedValue({} as never);
  render(<Marks />);
  await waitFor(() => expect(api.shareImports).toHaveBeenCalled());
  expect(screen.getByTestId("marks")).toHaveTextContent("");
});

test("import: an evicted preview asks to preview again with its own words", async () => {
  const { dialog } = await preview();
  vi.mocked(api.shareConfirm).mockRejectedValue(new ApiError(409, "evicted", { reason: "evicted" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  expect(await within(dialog).findByText(/cleared to make room/)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: /Preview again/ })).toBeEnabled();
});

test("import: a full server is not reported as a refused file", async () => {
  vi.mocked(api.sharePreview).mockRejectedValue(
    new ApiError(503, "Too many imports are waiting to finish."),
  );
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), {
    target: { files: [new File(["x"], "kit.battlelab")] },
  });
  expect(await within(dialog).findByText("Imports can't be previewed right now.")).toBeInTheDocument();
  expect(within(dialog).queryByText("This file can't be imported.")).toBeNull();
});

test("import: a lost confirm answer freezes the selection; only the same selection is sent again", async () => {
  const { dialog, onImported } = await preview();
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(new ApiError(503, "Share storage is unavailable."));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  expect(await within(dialog).findByText("The import may have been applied.")).toBeInTheDocument();
  expect(within(dialog).getByLabelText("Import Skill b")).toBeDisabled();
  expect(within(dialog).queryByRole("button", { name: "Back" })).toBeNull();
  vi.mocked(api.shareConfirm).mockResolvedValueOnce({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: PREVIEW.items.map((i) => ({ key: i.key, kind: i.kind, id: i.id, title: i.title, status: "created" as const, dest: i.dest, reason: null })),
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Try again (same selection)" }));
  await within(dialog).findByTestId("share-result-row-playbook:p");
  const calls = vi.mocked(api.shareConfirm).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[1]).toEqual(calls[0]);
  fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
  expect(onImported).toHaveBeenCalled();
});

test("import: a 409 selection says the preview was already confirmed and offers nothing to re-import", async () => {
  const { dialog, onImported } = await preview();
  vi.mocked(api.shareConfirm).mockRejectedValue(new ApiError(409, "already confirmed", { reason: "selection" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  expect(await within(dialog).findByText("This preview was already confirmed.")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Import \d/ })).toBeNull();
  expect(within(dialog).queryByRole("button", { name: /Preview again/ })).toBeNull();
  fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" }).at(-1)!);
  expect(onImported).toHaveBeenCalled();
});

test("import: Preview again after a partial import starts with the created items unticked and says why", async () => {
  const { dialog } = await preview();
  vi.mocked(api.shareConfirm)
    .mockResolvedValueOnce({
      token: PREVIEW.token,
      bundle: PREVIEW.bundle.digest,
      repreview: null,
      items: [
        { key: "skill:a", kind: "skill", id: "a", title: "a", status: "created", dest: "a-2", reason: null },
        { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created", dest: "b", reason: null },
        { key: "playbook:p", kind: "playbook", id: "p", title: "P", status: "failed", dest: "p", reason: "busy" },
      ],
    })
    .mockResolvedValueOnce({
      token: PREVIEW.token,
      bundle: PREVIEW.bundle.digest,
      repreview: "expired",
      items: [
        { key: "skill:a", kind: "skill", id: "a", title: "a", status: "created", dest: "a-2", reason: null },
        { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created", dest: "b", reason: null },
        { key: "playbook:p", kind: "playbook", id: "p", title: "P", status: "stale", dest: null, reason: "expired" },
      ],
    });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  fireEvent.click(await within(dialog).findByRole("button", { name: "Retry failed items" }));
  // A real server answers a fresh token for a confirmed preview's file.
  vi.mocked(api.sharePreview).mockResolvedValueOnce({ ...PREVIEW, token: "f".repeat(32) });
  fireEvent.click(await within(dialog).findByRole("button", { name: "Preview again" }));
  await within(dialog).findByText(/Not signed/);
  expect(within(dialog).getByLabelText("Import Skill a")).not.toBeChecked();
  expect(within(dialog).getByLabelText("Import Skill b")).not.toBeChecked();
  expect(within(dialog).getByTestId("share-preview-row-skill:a")).toHaveTextContent("Already imported from this file as a-2");
  // The playbook failed, so whether it was published is unknown: nothing starts ticked, and the
  // preview says why.
  expect(within(dialog).getByLabelText("Import Playbook P")).not.toBeChecked();
  expect(within(dialog).getByText("An earlier import of this file may have been applied.")).toBeInTheDocument();
  // Ticking the playbook alone is refused with the created skill named.
  fireEvent.click(within(dialog).getByLabelText("Import Playbook P"));
  const row = within(dialog).getByTestId("share-preview-row-playbook:p");
  expect(row).toHaveAttribute("data-state", "refused");
  expect(row).toHaveTextContent("already imported as a-2");
});

const CREATED_AB = {
  token: PREVIEW.token,
  bundle: PREVIEW.bundle.digest,
  repreview: null,
  items: [
    { key: "skill:a", kind: "skill", id: "a", title: "a", status: "created" as const, dest: "a-2", reason: null },
    { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created" as const, dest: "b", reason: null },
    { key: "playbook:p", kind: "playbook", id: "p", title: "P", status: "failed" as const, dest: "p", reason: "busy" },
  ],
};

async function partialThen(dialog: HTMLElement) {
  vi.mocked(api.shareConfirm).mockResolvedValueOnce(CREATED_AB);
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  await within(dialog).findByTestId("share-result-row-skill:a");
  vi.mocked(api.sharePreview).mockResolvedValue({ ...PREVIEW, token: "f".repeat(32) });
}

test("import: Back, then the same file again, still never re-ticks what was created", async () => {
  const { dialog } = await preview();
  await partialThen(dialog);
  fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
  fireEvent.click(screen.getByRole("button", { name: "Import…" })); // close and reopen
  const again = await screen.findByTestId("share-import");
  fireEvent.change(within(again).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  await within(again).findByText(/Not signed/);
  expect(within(again).getByLabelText("Import Skill a")).not.toBeChecked();
  fireEvent.click(within(again).getByRole("button", { name: "Back" }));
  fireEvent.change(within(again).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  await within(again).findByText(/Not signed/);
  expect(within(again).getByLabelText("Import Skill a")).not.toBeChecked();
  expect(within(again).getByLabelText("Import Skill b")).not.toBeChecked();
  expect(within(again).getByTestId("share-preview-row-skill:a")).toHaveTextContent("Already imported from this file as a-2");
});

test("import: copies the library says came from this file start a fresh preview with nothing ticked", async () => {
  vi.mocked(api.shareImports).mockResolvedValue({
    imports: { "skill:a-2": { bundle: PREVIEW.bundle.digest, revision: "r" } },
  });
  const { dialog } = await preview();
  expect(within(dialog).getByText("1 copy was imported from this file before.")).toBeInTheDocument();
  for (const label of ["Import Skill a", "Import Skill b", "Import Playbook P"])
    expect(within(dialog).getByLabelText(label)).not.toBeChecked();
});

test("import: after an unclear confirm, a fresh preview of the file starts with nothing ticked", async () => {
  const { dialog } = await preview();
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(new ApiError(503, "Share storage is unavailable."));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  await within(dialog).findByText("The import may have been applied.", undefined, { timeout: 5000 });
  await waitFor(() =>
    expect(within(dialog).getByRole("button", { name: "Try again (same selection)" })).toHaveFocus(),
  );
  // A refusal of the retry keeps it frozen.
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(new ApiError(422, "odd"));
  fireEvent.click(within(dialog).getByRole("button", { name: "Try again (same selection)" }));
  await within(dialog).findByText("The import may have been applied.", undefined, { timeout: 5000 });
  expect(within(dialog).getByLabelText("Import Skill b")).toBeDisabled();
  fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" }).at(-1)!);
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const again = await screen.findByTestId("share-import", undefined, { timeout: 5000 });
  vi.mocked(api.sharePreview).mockResolvedValue({ ...PREVIEW, token: "f".repeat(32) });
  fireEvent.change(within(again).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  await within(again).findByText("An earlier import of this file may have been applied.", undefined, { timeout: 5000 });
  expect(within(again).getByLabelText("Import Skill a")).not.toBeChecked();
});

test("import: an unreadable confirm answer is treated as unclear, never a crash", async () => {
  const { dialog } = await preview();
  vi.mocked(api.shareConfirm).mockResolvedValueOnce({} as never);
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 3 items" }));
  expect(await within(dialog).findByText("The import may have been applied.")).toBeInTheDocument();
});

test("export: the dialog warns that pasted secrets travel with the text, and promises nothing else", async () => {
  vi.mocked(api.shareCatalog).mockResolvedValue({
    imports: {},
    items: [{ kind: "skill", id: "a", title: "a", depends_on: [], exportable: true, reason: null, source: "local" }],
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Export…" }));
  const dialog = await screen.findByTestId("share-export");
  expect(await within(dialog).findByText("Check the text for secrets before you share it.")).toBeInTheDocument();
  expect(within(dialog).queryByText(/never leave this install/)).toBeNull();
  expect(within(dialog).queryByText(/no secret values/)).toBeNull();
});

test("import: a file the preview couldn't show needs an explicit acknowledgement before Import", async () => {
  vi.mocked(api.sharePreview).mockResolvedValue({
    ...PREVIEW,
    items: PREVIEW.items.map((i) =>
      i.key === "skill:a" ? { ...i, files: [{ path: "SKILL.md", size: 2_000_000, omitted: true }] } : i,
    ),
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  await within(dialog).findByText(/Not signed/);
  const go = within(dialog).getByRole("button", { name: "Import 3 items" });
  expect(go).toBeDisabled();
  fireEvent.click(within(dialog).getByRole("checkbox", { name: /couldn't be shown here/ }));
  expect(go).toBeEnabled();
  // Deselecting the unreadable item removes the need; re-ticking it asks again.
  fireEvent.click(within(dialog).getByLabelText("Import Skill a"));
  expect(within(dialog).queryByTestId("share-blind-ack")).toBeNull();
  fireEvent.click(within(dialog).getByLabelText("Import Skill a"));
  expect(within(dialog).getByRole("checkbox", { name: /couldn't be shown here/ })).not.toBeChecked();
  expect(within(dialog).getByRole("button", { name: "Import 3 items" })).toBeDisabled();
  expect(screen.getByTestId("share-preview-summary")).toHaveTextContent("1 unreviewed file needs your OK");
});

test("import: when the library's import record can't be read, nothing starts ticked", async () => {
  vi.mocked(api.shareImports).mockRejectedValue(new ApiError(503, "sharing is busy; try again"));
  const { dialog } = await preview();
  expect(within(dialog).getByText("Couldn't check whether this file was imported before.")).toBeInTheDocument();
  for (const label of ["Import Skill a", "Import Skill b", "Import Playbook P"])
    expect(within(dialog).getByLabelText(label)).not.toBeChecked();
});

test("import: an import record answered without its list (a stub, `{}`) is unknown: nothing starts ticked", async () => {
  vi.mocked(api.shareImports).mockResolvedValue({} as never);
  const { dialog } = await preview();
  expect(within(dialog).getByText("Couldn't check whether this file was imported before.")).toBeInTheDocument();
  expect(within(dialog).getByLabelText("Import Skill a")).not.toBeChecked();
});

test("import: a reused skill is listed as already here, needs no tick, and satisfies what needs it (#1714 P2)", async () => {
  const reused: SharePreview = {
    ...PREVIEW,
    items: [
      { ...PREVIEW.items[0], dest: "a", status: "reused" },
      PREVIEW.items[1],
      { ...PREVIEW.items[2], uses: [] },
    ],
  };
  vi.mocked(api.sharePreview).mockResolvedValue(reused);
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  const row = await within(dialog).findByTestId("share-preview-row-skill:a");
  expect(row).toHaveAttribute("data-state", "reused");
  expect(row).toHaveTextContent("Reused");
  expect(within(row).queryByRole("checkbox")).toBeNull();
  expect(within(dialog).getByRole("region", { name: "Already here" })).toContainElement(row);
  expect(within(dialog).getByTestId("share-preview-row-playbook:p")).toHaveAttribute("data-state", "import");
  expect(screen.getByTestId("share-preview-summary")).toHaveTextContent("2 to import · 1 reused");
  vi.mocked(api.shareConfirm).mockResolvedValue({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: [
      { key: "skill:a", kind: "skill", id: "a", title: "a", status: "reused", dest: "a", reason: null },
      { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created", dest: "b", reason: null },
      { key: "playbook:p", kind: "playbook", id: "p", title: "P", status: "created", dest: "p", reason: null },
    ],
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  await waitFor(() => expect(api.shareConfirm).toHaveBeenCalledWith(PREVIEW.token, ["skill:b", "playbook:p"]));
  const result = await within(dialog).findByTestId("share-result-row-skill:a");
  expect(result).toHaveTextContent("Reused");
  expect(result).toHaveTextContent("a (yours, reused)");
  expect(within(result).queryByRole("link")).toBeNull();
  expect(within(dialog).getByRole("status")).toHaveTextContent("2 of the 2 items you chose were imported");
});


// ---- specialists (#1714 P2) ---------------------------------------------------------------------

afterEach(() => resetRoster());

const SPEC = (over: Partial<NonNullable<SharePreviewItem["specialist"]>> = {}): SharePreviewItem => ({
  key: "specialist:rev",
  kind: "specialist",
  id: "rev",
  title: "Reviewer",
  dest: "rev",
  status: "new",
  reason: null,
  depends_on: ["skill:b"],
  uses: [],
  files: [{ path: "specialist.json", size: 10, text: "{}" }],
  specialist: {
    title: "Reviewer",
    summary: "",
    capacity: 1,
    skills: ["b"],
    suggests: { engine: "claude", model: "claude-opus-5", effort: null },
    settled: { engine: "claude", model: "claude-opus-5", effort: null },
    settle_reason: null,
    grade: null,
    ...over,
  },
});

async function previewWith(item: SharePreviewItem) {
  setRoster(fixture.engines as EngineInfo[]);
  vi.mocked(api.sharePreview).mockResolvedValue({ ...PREVIEW, items: [PREVIEW.items[1], item] });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  await within(dialog).findByTestId("share-settle-specialist:rev");
  return dialog;
}

test("import: a specialist whose suggestion holds here is ready, and confirm sends that choice", async () => {
  const dialog = await previewWith(SPEC());
  const fields = within(dialog).getByTestId("share-settle-specialist:rev");
  expect(fields).toHaveTextContent("The suggested agent and model work here.");
  expect(within(fields).getByLabelText("Agent for Reviewer")).toHaveValue("claude");
  expect(within(fields).getByLabelText("Model for Reviewer")).toHaveValue("claude-opus-5");
  expect(within(dialog).getByTestId("share-preview-row-specialist:rev")).toHaveTextContent("version 1 · capacity 1 · pins b");
  vi.mocked(api.shareConfirm).mockResolvedValue({ token: PREVIEW.token, bundle: PREVIEW.bundle.digest, repreview: null, items: [] });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  await waitFor(() =>
    expect(api.shareConfirm).toHaveBeenCalledWith(PREVIEW.token, ["skill:b", "specialist:rev"], {
      "specialist:rev": { engine: "claude", model: "claude-opus-5", effort: null },
    }),
  );
});

test("import: a suggestion that doesn't hold here must be settled before Import is offered", async () => {
  const dialog = await previewWith(
    SPEC({
      suggests: { engine: "codex-api", model: "gpt-5", effort: "high" },
      settled: null,
      settle_reason: "Suggested codex-api · gpt-5 · high, but codex-api cannot start a session on this host. Choose what it runs on here.",
    }),
  );
  const row = within(dialog).getByTestId("share-preview-row-specialist:rev");
  expect(row).toHaveTextContent("Choose agent");
  expect(row).toHaveTextContent("codex-api cannot start a session on this host");
  expect(screen.getByTestId("share-preview-summary")).toHaveTextContent("1 needs an agent");
  const importButton = within(dialog).getByRole("button", { name: "Import 2 items" });
  expect(importButton).toBeDisabled();
  // An agent that can't load skills is not offered for a specialist that pins skills.
  const gemini = within(row).getByRole("option", { name: /gemini · cannot load skills/ }) as HTMLOptionElement;
  expect(gemini.disabled).toBe(true);
  fireEvent.change(within(row).getByLabelText("Agent for Reviewer"), { target: { value: "codex" } });
  expect(row).toHaveTextContent("You chose codex. The file suggested codex-api · gpt-5 · high.");
  expect(importButton).toBeEnabled();
  vi.mocked(api.shareConfirm).mockResolvedValue({ token: PREVIEW.token, bundle: PREVIEW.bundle.digest, repreview: null, items: [] });
  fireEvent.click(importButton);
  await waitFor(() =>
    expect(api.shareConfirm).toHaveBeenCalledWith(PREVIEW.token, ["skill:b", "specialist:rev"], {
      "specialist:rev": { engine: "codex", model: "default", effort: null },
    }),
  );
});

test("import: a choice the server refuses (422 settle) is shown on its row, nothing was imported, and it stays editable", async () => {
  const dialog = await previewWith(SPEC());
  vi.mocked(api.shareConfirm).mockRejectedValue(
    new ApiError(422, "Nothing was imported: what a specialist runs on doesn't work here. Change it, then import again.", {
      detail: "Nothing was imported",
      reason: "settle",
      items: { "specialist:rev": "claude doesn't offer the model claude-opus-5 now." },
    }),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  const fields = within(dialog).getByTestId("share-settle-specialist:rev");
  expect(await within(fields).findByRole("alert")).toHaveTextContent("claude doesn't offer the model claude-opus-5 now.");
  expect(within(dialog).getByText(/Nothing was imported: what a specialist runs on/)).toBeInTheDocument();
  await waitFor(() => expect(within(fields).getByLabelText("Agent for Reviewer")).toHaveFocus());
  const model = within(fields).getByLabelText("Model for Reviewer");
  expect(model).toBeEnabled();
  fireEvent.change(model, { target: { value: "claude-sonnet-5" } });
  expect(within(fields).queryByRole("alert")).toBeNull();
});

test("import: the settled specialist's result links to its new page", async () => {
  const dialog = await previewWith(SPEC());
  vi.mocked(api.shareConfirm).mockResolvedValue({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: [
      { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created", dest: "b", reason: null },
      { key: "specialist:rev", kind: "specialist", id: "rev", title: "Reviewer", status: "created", dest: "rev-2", reason: null },
    ],
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  const row = await within(dialog).findByTestId("share-result-row-specialist:rev");
  expect(within(row).getByRole("link", { name: /Open/ })).toHaveAttribute("href", "/library/specialists/rev-2");
});

// ---- independent review of #1812 ----------------------------------------------------------------

const SETTLE_422 = () =>
  new ApiError(422, "Nothing was imported: what a specialist runs on doesn't work here. Change it, then import again.", {
    reason: "settle",
    items: { "specialist:rev": "claude doesn't offer the model claude-opus-5 now." },
  });

test("import: after a 422 settle, Import waits for a changed choice, the banner clears with the fix, and a repeat refocuses", async () => {
  const dialog = await previewWith(SPEC());
  const fields = within(dialog).getByTestId("share-settle-specialist:rev");
  const importButton = within(dialog).getByRole("button", { name: "Import 2 items" });
  vi.mocked(api.shareConfirm).mockRejectedValue(SETTLE_422());
  fireEvent.click(importButton);
  await within(fields).findByRole("alert");
  expect(importButton).toBeDisabled(); // the refused choice is unchanged
  expect(within(dialog).getByText("Import failed.")).toBeInTheDocument();
  const model = within(fields).getByLabelText("Model for Reviewer");
  fireEvent.change(model, { target: { value: "claude-sonnet-5" } });
  expect(importButton).toBeEnabled();
  expect(within(dialog).queryByText("Import failed.")).toBeNull();
  fireEvent.change(model, { target: { value: "claude-opus-5" } });
  (document.activeElement as HTMLElement | null)?.blur();
  fireEvent.click(importButton); // the identical refusal again
  await waitFor(() => expect(within(fields).getByLabelText("Agent for Reviewer")).toHaveFocus());
});

test("import: a renamed skill pinned only by a specialist says specialists follow it, not playbooks", async () => {
  const spec = { ...SPEC(), uses: [{ from: "b", to: "b-2" }] };
  setRoster(fixture.engines as EngineInfo[]);
  vi.mocked(api.sharePreview).mockResolvedValue({
    ...PREVIEW,
    items: [{ ...PREVIEW.items[1], dest: "b-2", status: "renamed" }, spec],
  });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Import…" }));
  const dialog = await screen.findByTestId("share-import");
  fireEvent.change(within(dialog).getByTestId("share-file"), { target: { files: [new File(["x"], "kit.battlelab")] } });
  const row = await within(dialog).findByTestId("share-preview-row-skill:b");
  expect(row).toHaveTextContent("specialists in this file that use it are pointed at b-2");
  expect(row).not.toHaveTextContent("playbooks");
});

test("import: an unticked specialist's choice is never sent", async () => {
  const dialog = await previewWith(SPEC());
  fireEvent.click(within(dialog).getByLabelText("Import Specialist Reviewer"));
  vi.mocked(api.shareConfirm).mockResolvedValue({ token: PREVIEW.token, bundle: PREVIEW.bundle.digest, repreview: null, items: [] });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 1 item" }));
  await waitFor(() => expect(api.shareConfirm).toHaveBeenCalled());
  expect(vi.mocked(api.shareConfirm).mock.calls[0]).toEqual([PREVIEW.token, ["skill:b"]]);
});

test("import: after a lost answer, the retry re-sends exactly the same choices", async () => {
  const dialog = await previewWith(SPEC());
  fireEvent.change(within(dialog).getByLabelText("Agent for Reviewer"), { target: { value: "codex" } });
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(new ApiError(503, "Share storage is unavailable."));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  const retry = await within(dialog).findByRole("button", { name: "Try again (same selection)" });
  expect(within(dialog).getByLabelText("Agent for Reviewer")).toBeDisabled();
  vi.mocked(api.shareConfirm).mockResolvedValue({ token: PREVIEW.token, bundle: PREVIEW.bundle.digest, repreview: null, items: [] });
  fireEvent.click(retry);
  await waitFor(() => expect(api.shareConfirm).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(api.shareConfirm).mock.calls;
  expect(calls[1]).toEqual(calls[0]);
  expect(calls[0][2]).toEqual({ "specialist:rev": { engine: "codex", model: "default", effort: null } });
});

test("import: a 422 settle on the retry of a lost answer keeps the dialog frozen", async () => {
  const dialog = await previewWith(SPEC());
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(new ApiError(503, "Share storage is unavailable."));
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  const retry = await within(dialog).findByRole("button", { name: "Try again (same selection)" });
  vi.mocked(api.shareConfirm).mockRejectedValueOnce(SETTLE_422());
  fireEvent.click(retry);
  await waitFor(() => expect(api.shareConfirm).toHaveBeenCalledTimes(2));
  expect(await within(dialog).findByRole("button", { name: "Try again (same selection)" })).toBeInTheDocument();
  expect(within(dialog).getByText(/The retry was refused/)).toBeInTheDocument();
  expect(within(dialog).getByLabelText("Agent for Reviewer")).toBeDisabled();
});

test("import: a failure recorded for good (a pinned skill moved) offers Preview again, never Retry", async () => {
  const dialog = await previewWith(SPEC());
  vi.mocked(api.shareConfirm).mockResolvedValue({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: [
      { key: "skill:b", kind: "skill", id: "b", title: "b", status: "created", dest: "b", reason: null, retry: false },
      {
        key: "specialist:rev",
        kind: "specialist",
        id: "rev",
        title: "Reviewer",
        status: "failed",
        dest: "rev",
        reason: "Nothing was written: a skill it pins changed or can't be read during the import. Preview the file again to import it.",
        retry: false,
      },
    ],
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  await within(dialog).findByTestId("share-result-row-specialist:rev");
  expect(within(dialog).queryByRole("button", { name: "Retry failed items" })).toBeNull();
  vi.mocked(api.sharePreview).mockClear();
  fireEvent.click(within(dialog).getByRole("button", { name: "Preview again" }));
  await waitFor(() => expect(api.sharePreview).toHaveBeenCalledTimes(1));
  expect(api.shareConfirm).toHaveBeenCalledTimes(1); // the same token is never confirmed again
});

test("import: a retriable failure still offers Retry failed items (and an older answer without `retry` counts as one)", async () => {
  const dialog = await previewWith(SPEC());
  vi.mocked(api.shareConfirm).mockResolvedValue({
    token: PREVIEW.token,
    bundle: PREVIEW.bundle.digest,
    repreview: null,
    items: [
      { key: "skill:b", kind: "skill", id: "b", title: "b", status: "failed", dest: "b", reason: "Nothing was written: a storage error (EIO). Confirm again to retry this item." },
      { key: "specialist:rev", kind: "specialist", id: "rev", title: "Reviewer", status: "skipped", dest: "rev", reason: "Not attempted." },
    ],
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Import 2 items" }));
  expect(await within(dialog).findByRole("button", { name: "Retry failed items" })).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Preview again" })).toBeNull();
});
