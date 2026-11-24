/** Shared artifact repositories (#2347 P1): the New repository dialog shows the server's words on a
 *  refusal, "Add an artifact…" adds the picked artifact as a new item or (same kind only) a new
 *  revision, the Repositories tab's empty state and list, and a repository's page: storage, items,
 *  Open as the sandboxed item link, Remove behind a confirm. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

import { ApiError } from "../../lib/api";
import {
  ARTIFACT_REPOS_PATH,
  ARTIFACT_REPO_ROUTE,
  artifactRepoPath,
} from "../../lib/routes";
import type {
  Artifact,
  ArtifactRepo,
  ArtifactRepoItem,
} from "../../types/artifacts";
import ArtifactRepoPage from "../../routes/ArtifactRepo";
import ArtifactRepos from "../../routes/ArtifactRepos";
import { AddArtifactDialog, NewRepoDialog } from "./RepoDialogs";
import { addedByLabel, repoFacts, storageLabel } from "./repoModel";

vi.mock("../../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...real,
    api: {
      artifacts: vi.fn(),
      artifactRepos: vi.fn(),
      artifactRepo: vi.fn(),
      artifactRepoItems: vi.fn(),
      createArtifactRepo: vi.fn(),
      deleteArtifactRepo: vi.fn(),
      addToArtifactRepo: vi.fn(),
      removeFromArtifactRepo: vi.fn(),
      projectEntities: vi.fn().mockResolvedValue({ projects: [] }),
      missions: vi.fn().mockResolvedValue({ missions: [] }),
      session: vi.fn().mockResolvedValue({ title: "Brand refresh" }),
    },
  };
});
const { api } = await import("../../lib/api");

const REPO: ArtifactRepo = {
  id: "4821-3307-9154-6602",
  name: "design-assets",
  description: "Files several BattleLabs use",
  quota_bytes: 2 * 2 ** 30,
  bytes: 412 * 2 ** 20,
  items: 2,
  created_at: 0,
  updated_at: 0,
};

function item(id: string, title: string, extra: Partial<ArtifactRepoItem> = {}): ArtifactRepoItem {
  return {
    id,
    repository_id: REPO.id,
    title,
    why: `Why ${title}`,
    kind: "file",
    revision: 1,
    added_by: { kind: "operator" },
    source_artifact: null,
    created_at: 0,
    updated_at: 0,
    files: 1,
    bytes: 2_100_000,
    entry: "hero.png",
    url: `/artifacts/i/${id}/`,
    ...extra,
  };
}

function art(id: string, title: string, kind: "file" | "site"): Artifact {
  return {
    id,
    title,
    why: "w",
    kind,
    revision: 2,
    created_at: 0,
    updated_at: 0,
    origin: { engine: "claude", session_key: "claude:s", cwd: "/w", project_id: null, mission_id: null },
    visibility: "private",
    url: `/artifacts/${id}/`,
    public_url: null,
    public_since: null,
    files: 1,
    bytes: 1000,
    entry: kind === "site" ? "index.html" : "a.pdf",
  };
}

afterEach(() => vi.clearAllMocks());

test("the wording: facts, storage and who added an item", () => {
  expect(storageLabel(412 * 2 ** 20, 2 * 2 ** 30)).toBe("412 MB of 2 GB");
  expect(repoFacts({ ...REPO, items: 1 })).toBe("1 item · 412 MB of 2 GB");
  expect(addedByLabel({ kind: "operator" })).toEqual({ who: "this BattleLab", how: "you" });
  expect(addedByLabel({ kind: "session", session_key: "claude:x" }, "Brand refresh")).toEqual({
    who: "this BattleLab",
    how: "Brand refresh",
  });
  expect(addedByLabel({ kind: "session", session_key: "claude:x" }).how).toBe("claude:x");
});

test("New repository sends the name and description and shows the server's refusal", async () => {
  const onCreated = vi.fn();
  vi.mocked(api.createArtifactRepo)
    .mockRejectedValueOnce(new ApiError(422, "A repository name is 1–64 lowercase letters…"))
    .mockResolvedValueOnce(REPO);
  render(<NewRepoDialog onClose={() => {}} onCreated={onCreated} />);
  const create = screen.getByRole("button", { name: "Create repository" });
  expect(create).toBeDisabled();
  await userEvent.type(screen.getByRole("textbox", { name: /Name/ }), "Design Assets");
  await userEvent.type(screen.getByRole("textbox", { name: /Description/ }), "shared");
  await userEvent.click(create);
  expect(await screen.findByRole("alert")).toHaveTextContent("lowercase");
  expect(onCreated).not.toHaveBeenCalled();
  await userEvent.clear(screen.getByRole("textbox", { name: /Name/ }));
  await userEvent.type(screen.getByRole("textbox", { name: /Name/ }), "design-assets");
  await userEvent.click(create);
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(REPO));
  expect(api.createArtifactRepo).toHaveBeenLastCalledWith("design-assets", "shared");
});

test("Add an artifact adds the pick as a new item, or a new revision of a same-kind item", async () => {
  const onAdded = vi.fn();
  const items = [item("1111-2222-3333-4444", "Pricing hero"), item("5555-6666-7777-8888", "Screens", { kind: "site" })];
  vi.mocked(api.artifacts).mockResolvedValue({
    items: [art("1000-2000-3000-4000", "Brand guidelines", "file"), art("1000-2000-3000-5000", "Onboarding", "site")],
    total: 2,
    next_cursor: null,
  });
  vi.mocked(api.addToArtifactRepo).mockResolvedValue(items[0]);
  render(<AddArtifactDialog repo={REPO} items={items} onClose={() => {}} onAdded={onAdded} />);
  const add = screen.getByRole("button", { name: "Add to repository" });
  expect(add).toBeDisabled();
  await userEvent.click(await screen.findByRole("radio", { name: /Brand guidelines/ }));
  // Only the file item is offered as a revision target for a file artifact.
  const as = screen.getByRole("combobox", { name: /Add as/ });
  const options = within(as).getAllByRole("option").map((o) => o.textContent);
  expect(options).toEqual(["A new item", "A new revision of “Pricing hero” (r1)"]);
  await userEvent.click(add);
  await waitFor(() => expect(onAdded).toHaveBeenCalled());
  expect(api.addToArtifactRepo).toHaveBeenLastCalledWith(REPO.id, "1000-2000-3000-4000", undefined);
  await userEvent.selectOptions(as, "1111-2222-3333-4444");
  await userEvent.click(add);
  await waitFor(() =>
    expect(api.addToArtifactRepo).toHaveBeenLastCalledWith(
      REPO.id,
      "1000-2000-3000-4000",
      "1111-2222-3333-4444",
    ),
  );
});

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={ARTIFACT_REPOS_PATH} element={<ArtifactRepos />} />
        <Route path={ARTIFACT_REPO_ROUTE} element={<ArtifactRepoPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

test("the Repositories tab: the empty state, then the hosted list with its facts", async () => {
  vi.mocked(api.artifactRepos).mockResolvedValueOnce({ repositories: [], quota_default: 2 * 2 ** 30 });
  const first = at(ARTIFACT_REPOS_PATH);
  expect(await screen.findByTestId("repos-none")).toHaveTextContent("No repositories here yet");
  expect(screen.getByRole("tab", { name: "Repositories" })).toHaveAttribute("aria-selected", "true");
  first.unmount();
  vi.mocked(api.artifactRepos).mockResolvedValueOnce({ repositories: [REPO], quota_default: 2 * 2 ** 30 });
  at(ARTIFACT_REPOS_PATH);
  const link = await screen.findByRole("link", { name: "design-assets" });
  expect(link).toHaveAttribute("href", artifactRepoPath(REPO.id));
  expect(screen.getByText("2 items · 412 MB of 2 GB")).toBeInTheDocument();
});

test("a repository's page: storage, items, Open as the item link, Remove behind a confirm", async () => {
  const items = [
    item("1111-2222-3333-4444", "Pricing hero"),
    item("5555-6666-7777-8888", "Onboarding screens", {
      kind: "site",
      revision: 4,
      entry: "index.html",
      added_by: { kind: "session", session_key: "claude:abc" },
    }),
  ];
  vi.mocked(api.artifactRepo).mockResolvedValue(REPO);
  vi.mocked(api.artifactRepoItems).mockResolvedValue({ items, total: 2, next_cursor: null });
  vi.mocked(api.removeFromArtifactRepo).mockResolvedValue({ removed: items[0].id });
  at(artifactRepoPath(REPO.id));
  expect(await screen.findByRole("heading", { name: "design-assets" })).toBeInTheDocument();
  expect(screen.getByTestId("repo-storage")).toHaveTextContent("412 MB of 2 GB");
  expect(screen.getByRole("meter", { name: "Repository storage used" })).toHaveAttribute(
    "aria-valuenow",
    "20",
  );
  const open = screen.getByRole("link", { name: "Open Onboarding screens in a new tab" });
  expect(open.getAttribute("href")).toMatch(/\/artifacts\/i\/5555-6666-7777-8888\/$/);
  expect(open).toHaveAttribute("rel", "noopener noreferrer");
  const row = document.querySelector("[data-item='5555-6666-7777-8888']") as HTMLElement;
  expect(within(row).getAllByText("Site")[0]).toBeInTheDocument();
  expect(await within(row).findByText(/Brand refresh/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Remove Pricing hero from design-assets" }));
  const dialog = await screen.findByTestId("repo-confirm");
  expect(dialog).toHaveTextContent("Remove “Pricing hero” from design-assets?");
  await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
  await waitFor(() =>
    expect(api.removeFromArtifactRepo).toHaveBeenCalledWith(REPO.id, "1111-2222-3333-4444"),
  );
  expect(await screen.findByRole("status")).toHaveTextContent("is removed from design-assets");
});

test("a repository with no items says how to add one, and an unknown one says so", async () => {
  vi.mocked(api.artifactRepo).mockResolvedValueOnce({ ...REPO, items: 0, bytes: 0 });
  vi.mocked(api.artifactRepoItems).mockResolvedValueOnce({ items: [], total: 0, next_cursor: null });
  const first = at(artifactRepoPath(REPO.id));
  expect(await screen.findByTestId("repo-empty")).toHaveTextContent("Add an artifact…");
  first.unmount();
  vi.mocked(api.artifactRepo).mockRejectedValueOnce(new ApiError(404, "There is no such repository."));
  vi.mocked(api.artifactRepoItems).mockRejectedValueOnce(new ApiError(404, "nope"));
  at(artifactRepoPath("9999-9999-9999-9999"));
  expect(await screen.findByTestId("repo-missing")).toBeInTheDocument();
});
