import { afterEach, beforeEach, expect, test } from "vitest";
import { api, setApiFetch } from "./api";
import { projectPickerStore } from "./projectPickerStore";

const P = {
  id: "p-a",
  name: "Alpha",
  color: "",
  folders: ["/a"],
  default_folder: "/a",
  archived: false,
  created_at: 0,
};

beforeEach(() => projectPickerStore.reset());
afterEach(() => setApiFetch(null));

test("a list fetched before a mutation is dropped, not committed over it (#1436)", () => {
  const gen = projectPickerStore.generation();
  projectPickerStore.invalidate();
  expect(projectPickerStore.commit(gen, [P])).toBe(false);
  expect(projectPickerStore.list()).toBeNull();
  expect(projectPickerStore.commit(projectPickerStore.generation(), [P])).toBe(true);
  expect(projectPickerStore.list()).toEqual([P]);
});

test("the launch picker asks for the count-free list (#1436)", async () => {
  const urls: string[] = [];
  setApiFetch(async (url) => {
    urls.push(url);
    return new Response(JSON.stringify({ projects: [P] }), { status: 200 });
  });
  expect((await api.projectPickerEntities()).projects).toEqual([P]);
  expect(urls).toEqual(["/api/projects?counts=0"]);
});

const MUTATIONS: [string, () => Promise<unknown>][] = [
  ["create", () => api.createProject({ name: "N" })],
  ["rename", () => api.patchProject("p-a", { name: "M" })],
  ["archive", () => api.archiveProject("p-a")],
  ["unarchive", () => api.unarchiveProject("p-a")],
  ["delete", () => api.deleteProject("p-a")],
];

test.each(MUTATIONS)("a project %s invalidates the picker — on success and on failure (#1436)", async (_, call) => {
  setApiFetch(async () => new Response("{}", { status: 200 }));
  const before = projectPickerStore.generation();
  await call();
  const afterSuccess = projectPickerStore.generation();
  expect(afterSuccess).toBeGreaterThan(before); // a create also moves it once more, in `add`
  setApiFetch(async () => new Response(JSON.stringify({ detail: "nope" }), { status: 500 }));
  await call().catch(() => {});
  expect(projectPickerStore.generation()).toBe(afterSuccess + 1);
});

test("a created project joins the cached list at once, sorted by name (Hermes on #1437)", async () => {
  projectPickerStore.commit(projectPickerStore.generation(), [{ ...P, id: "p-z", name: "Zeta" }]);
  setApiFetch(async () => new Response(JSON.stringify({ ...P, id: "p-b", name: "beta" }), { status: 200 }));
  await api.createProject({ name: "beta" });
  expect(projectPickerStore.list()?.map((p) => p.id)).toEqual(["p-b", "p-z"]);
});

test("a list fetched before a create cannot overwrite the created project (#1437 review)", () => {
  const before = projectPickerStore.generation();
  projectPickerStore.add(P);
  expect(projectPickerStore.commit(before, [])).toBe(false);
  expect(projectPickerStore.list()).toEqual([P]);
});
