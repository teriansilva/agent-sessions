import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { PlaybookDetail } from "../../types/playbooks";
import type { DeploymentReview } from "../../types/playbookDeployment";
import {
  assignmentUnavailable,
  useProjectPlaybook,
} from "./useProjectPlaybook";

vi.mock("../../lib/api", () => ({
  api: Object.fromEntries(
    [
      "playbooks",
      "playbook",
      "playbookAuthoring",
      "reviewPlaybook",
      "confirmPlaybook",
      "createPlaybookTarget",
      "bindPlaybook",
      "applyPlaybook",
      "verifyPlaybook",
      "playbookDeployment",
    ].map((name) => [name, vi.fn()]),
  ),
}));
const detail = (): PlaybookDetail => ({
  id: "demo",
  ok: true,
  error: null,
  source: "bundled",
  editable: false,
  revision: "a".repeat(64),
  default: false,
  recovery_total: 0,
  files: {},
  documents: {
    "playbook.toml": {
      format: 2,
      variables: [
        { name: "region", kind: "text", type: "text" },
        { name: "token", kind: "secret", type: "text" },
      ],
    },
  },
});
const plan = (): DeploymentReview => ({
  digest: "b".repeat(64),
  deployment_id: "d-demo",
  destination: { path: "/tmp/project", device: 12, inode: 34 },
  variables: [],
  materials: [],
  changes: [],
  conflicts: [],
  targets: [],
  assignments: [],
  capability_requests: [],
  rituals: [],
});
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.playbooks).mockResolvedValue({
    playbooks: [detail()],
    default: null,
    recovery_total: 0,
  });
  vi.mocked(api.playbook).mockResolvedValue(detail());
  vi.mocked(api.playbookAuthoring).mockResolvedValue({ agents: [] } as never);
  vi.mocked(api.reviewPlaybook).mockResolvedValue(plan());
  vi.mocked(api.confirmPlaybook).mockResolvedValue({
    receipt: "review-receipt",
    digest: "b".repeat(64),
  });
  vi.mocked(api.createPlaybookTarget).mockResolvedValue({ path: "/tmp/project", device: 12, inode: 34, review_digest: "b".repeat(64) });
  vi.mocked(api.bindPlaybook).mockResolvedValue({
    receipt: "bound-receipt",
    digest: "c".repeat(64),
  });
  vi.mocked(api.applyPlaybook).mockResolvedValue({
    project_id: "p-same",
    state: "applied",
  });
  vi.mocked(api.verifyPlaybook).mockResolvedValue({
    deployment_id: "d-demo",
    ok: true,
    checks: {},
  });
  vi.mocked(api.playbookDeployment).mockResolvedValue({
    project_id: "p-same",
    state: "none",
  });
});
async function setup(create = true) {
  const hook = renderHook(
    ({ project }: { project?: string }) =>
      useProjectPlaybook("/tmp/project", create, project),
    { initialProps: {} },
  );
  await waitFor(() => expect(hook.result.current.gallery).not.toBeNull());
  act(() => hook.result.current.select("demo"));
  await waitFor(() => expect(hook.result.current.selectedReady).toBe(true));
  return hook;
}
describe("project assignment authority and recovery", () => {
  test("selection and input review perform no effects; default-derived targets need individual confirmation", async () => {
    const reviewed = plan();
    reviewed.targets = [
      {
        id: "connection:service",
        kind: "connection",
        args: { url: "https://example.test" },
        pending: {},
        default_variables: ["url"],
        requires_confirmation: true,
      },
    ];
    vi.mocked(api.reviewPlaybook).mockResolvedValue(reviewed);
    const { result } = await setup();
    act(() =>
      result.current.setChoices({
        token: { mode: "value", value: "fictional-private-input" },
      }),
    );
    await act(() => result.current.review());
    expect(result.current.canCreate).toBe(false);
    await act(async () => {
      expect(await result.current.prepare()).toBe(false);
    });
    expect(api.confirmPlaybook).not.toHaveBeenCalled();
    expect(api.createPlaybookTarget).not.toHaveBeenCalled();
    expect(api.bindPlaybook).not.toHaveBeenCalled();
    act(() => result.current.setConfirmed(["connection:service"]));
    await act(async () => {
      expect(await result.current.prepare()).toBe(true);
    });
    expect(api.confirmPlaybook).toHaveBeenCalledWith(
      "demo",
      expect.objectContaining({
        bindings: [
          { name: "token", kind: "secret", value: "fictional-private-input" },
        ],
      }),
      reviewed.digest,
      ["connection:service"],
    );
    expect(api.createPlaybookTarget).toHaveBeenCalledTimes(1);
    expect(api.applyPlaybook).not.toHaveBeenCalled();
  });
  test("a lost binding response retries the exact request and project, then clears transient secrets", async () => {
    vi.mocked(api.bindPlaybook).mockRejectedValueOnce(
      new Error("lost response"),
    );
    const { result, rerender } = await setup();
    act(() =>
      result.current.setChoices({
        token: { mode: "value", value: "fictional-private-input" },
      }),
    );
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    rerender({ project: "p-same" });
    await act(() => result.current.deploy("p-same"));
    expect(result.current.stage).toBe("bind");
    expect(api.applyPlaybook).not.toHaveBeenCalled();
    await act(() => result.current.deploy("p-same"));
    expect(vi.mocked(api.bindPlaybook).mock.calls[1]).toEqual(
      vi.mocked(api.bindPlaybook).mock.calls[0],
    );
    expect(api.applyPlaybook).toHaveBeenCalledWith(
      "p-same",
      "bound-receipt",
      expect.any(String),
    );
    expect(result.current.stage).toBe("done");
    expect(result.current.choices).toEqual({});
    expect(result.current.reviewed).toBeNull();
  });
  test("an interrupted apply retries the same operation without binding again", async () => {
    vi.mocked(api.applyPlaybook).mockRejectedValueOnce(
      new Error("lost response"),
    );
    vi.mocked(api.playbookDeployment).mockResolvedValue({
      project_id: "p-same",
      state: "interrupted",
      operation: "apply",
      files: [{ path: "README.md", state: "written" }],
    });
    const { result, rerender } = await setup(false);
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    rerender({ project: "p-same" });
    await act(() => result.current.deploy("p-same"));
    expect(result.current.status?.files?.[0].state).toBe("written");
    await act(() => result.current.recoverReview());
    expect(result.current.recovering).toBe(false);
    await act(() => result.current.deploy("p-same"));
    expect(api.bindPlaybook).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.applyPlaybook).mock.calls[1]).toEqual(
      vi.mocked(api.applyPlaybook).mock.calls[0],
    );
    expect(api.createPlaybookTarget).not.toHaveBeenCalled();
  });
  test("verification failure keeps applied state and re-verifies without new writes", async () => {
    vi.mocked(api.verifyPlaybook).mockRejectedValueOnce(
      new Error("unavailable"),
    );
    vi.mocked(api.playbookDeployment).mockResolvedValue({
      project_id: "p-same",
      state: "applied",
    });
    const { result } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    await act(() => result.current.deploy("p-same"));
    expect(result.current.status?.state).toBe("applied");
    await act(() => result.current.deploy("p-same"));
    expect(api.bindPlaybook).toHaveBeenCalledTimes(1);
    expect(api.applyPlaybook).toHaveBeenCalledTimes(1);
    expect(api.verifyPlaybook).toHaveBeenCalledTimes(2);
  });
  test("a changed input invalidates review and an old response cannot restore it", async () => {
    let finish!: (value: DeploymentReview) => void;
    vi.mocked(api.reviewPlaybook).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { result } = await setup();
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.review();
    });
    act(() =>
      result.current.setChoices({
        region: { mode: "value", value: "changed" },
      }),
    );
    await act(async () => {
      finish(plan());
      await pending;
    });
    expect(result.current.reviewed).toBeNull();
    expect(result.current.canCreate).toBe(false);
  });
  test("a prepared new folder retries its original receipt, not another absent-target review", async () => {
    const { result } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    await act(() => result.current.prepare());
    expect(api.confirmPlaybook).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.createPlaybookTarget).mock.calls[1]).toEqual(
      vi.mocked(api.createPlaybookTarget).mock.calls[0],
    );
  });
  test("refresh after a refused project create reviews the same created inode with the new source", async () => {
    const { result } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    vi.mocked(api.playbook).mockResolvedValue({ ...detail(), revision: "d".repeat(64) });
    await act(() => result.current.review(true));
    expect(api.reviewPlaybook).toHaveBeenLastCalledWith("demo", expect.objectContaining({ revision: "d".repeat(64) }));
    expect(vi.mocked(api.reviewPlaybook).mock.calls.at(-1)?.[1]).not.toHaveProperty("create");
    expect(result.current.reviewingCreatedTarget).toBe(true);
    await act(() => result.current.prepare());
    expect(api.createPlaybookTarget).toHaveBeenCalledTimes(1);
    expect(api.confirmPlaybook).toHaveBeenCalledTimes(2);
  });
  test("refresh refuses a replacement inode without accepting its plan", async () => {
    const { result } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    vi.mocked(api.reviewPlaybook).mockResolvedValue({ ...plan(), destination: { path: "/tmp/project", device: 12, inode: 999 } });
    await act(() => result.current.review(true));
    expect(result.current.error).toMatch(/folder was replaced/);
    expect(result.current.reviewed).toBeNull();
    expect(result.current.canCreate).toBe(false);
    expect(api.confirmPlaybook).toHaveBeenCalledTimes(1);
  });
  test("refresh never discards a receipt whose target-create response was lost", async () => {
    vi.mocked(api.createPlaybookTarget).mockRejectedValueOnce(new Error("response lost"));
    const { result } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    await act(() => result.current.review(true));
    expect(result.current.error).toMatch(/original review/);
    expect(api.reviewPlaybook).toHaveBeenCalledTimes(1);
    await act(() => result.current.prepare());
    expect(vi.mocked(api.createPlaybookTarget).mock.calls[1]).toEqual(vi.mocked(api.createPlaybookTarget).mock.calls[0]);
  });
  async function movable() {
    const hook = renderHook(
      ({ folder }: { folder: string }) => useProjectPlaybook(folder, true),
      { initialProps: { folder: "/tmp/project" } },
    );
    await waitFor(() => expect(hook.result.current.gallery).not.toBeNull());
    act(() => hook.result.current.select("demo"));
    await waitFor(() => expect(hook.result.current.selectedReady).toBe(true));
    return hook;
  }
  for (const replaced of [false, true])
    test(`a created folder refused by project create lets another folder be reviewed and created (replaced=${replaced})`, async () => {
      const { result, rerender } = await movable();
      await act(() => result.current.review());
      await act(() => result.current.prepare());
      if (replaced) {
        vi.mocked(api.reviewPlaybook).mockResolvedValueOnce({ ...plan(), destination: { path: "/tmp/project", device: 12, inode: 999 } });
        await act(() => result.current.review(true));
        expect(result.current.error).toMatch(/folder was replaced/);
      }
      vi.mocked(api.reviewPlaybook).mockResolvedValue({ ...plan(), destination: { path: "/tmp/other", device: 12, inode: 56 } });
      vi.mocked(api.createPlaybookTarget).mockResolvedValue({ path: "/tmp/other", device: 12, inode: 56, review_digest: "b".repeat(64) });
      rerender({ folder: "/tmp/other" });
      await act(() => result.current.review());
      expect(result.current.error).toBe("");
      expect(api.reviewPlaybook).toHaveBeenLastCalledWith("demo", expect.objectContaining({ destination: "/tmp/other", create: true }));
      expect(result.current.canCreate).toBe(true);
      await act(() => result.current.prepare());
      expect(api.createPlaybookTarget).toHaveBeenLastCalledWith("demo", expect.objectContaining({ destination: "/tmp/other", create: true }), "review-receipt");
      expect(api.confirmPlaybook).toHaveBeenCalledTimes(2);
    });
  test("a lost target-create response stays bound to its folder while another is reviewed", async () => {
    vi.mocked(api.createPlaybookTarget).mockRejectedValueOnce(new Error("response lost"));
    const { result, rerender } = await movable();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    rerender({ folder: "/tmp/other" });
    await act(() => result.current.review());
    expect(result.current.error).toBe("");
    expect(api.reviewPlaybook).toHaveBeenLastCalledWith("demo", expect.objectContaining({ destination: "/tmp/other" }));
    rerender({ folder: "/tmp/project" });
    await act(() => result.current.review());
    expect(result.current.error).toMatch(/original review/);
    expect(api.reviewPlaybook).toHaveBeenCalledTimes(2);
    expect(result.current.canCreate).toBe(true);
    await act(() => result.current.prepare());
    expect(api.confirmPlaybook).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.createPlaybookTarget).mock.calls[1]).toEqual(vi.mocked(api.createPlaybookTarget).mock.calls[0]);
  });
  test("leaving during bind prevents a later apply", async () => {
    let finish!: (value: { receipt: string; digest: string }) => void;
    vi.mocked(api.bindPlaybook).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const { result, unmount } = await setup();
    await act(() => result.current.review());
    await act(() => result.current.prepare());
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.deploy("p-same");
    });
    unmount();
    await act(async () => {
      finish({ receipt: "bound", digest: "c".repeat(64) });
      await pending;
    });
    expect(api.applyPlaybook).not.toHaveBeenCalled();
  });
});
test("a missing configured default remains No playbook with an explanation", async () => {
  vi.mocked(api.playbooks).mockResolvedValue({
    playbooks: [],
    default: "removed-default",
    recovery_total: 0,
  });
  const { result } = renderHook(() => useProjectPlaybook("/tmp/project", true));
  await waitFor(() =>
    expect(result.current.defaultNotice).toContain("removed-default"),
  );
  expect(result.current.id).toBeNull();
  expect(result.current.selectedReady).toBe(true);
});
test("secret-reference files explain the pre-project limitation", () => {
  const source = {
    ...detail(),
    files: { "settings.txt": "token_file={{secret_path:token}}" },
  };
  expect(assignmentUnavailable(source)).toContain("existing project");
  expect(assignmentUnavailable(source, false)).toBe("");
});
test("refreshing a secret-to-text declaration drops the transient value", async () => {
  const { result } = await setup();
  act(() =>
    result.current.setChoices({
      token: { mode: "value", value: "must-not-become-text" },
    }),
  );
  const fresh = detail();
  fresh.revision = "d".repeat(64);
  fresh.documents!["playbook.toml"].variables = [
    { name: "token", kind: "text", type: "text" },
  ];
  vi.mocked(api.playbook).mockResolvedValue(fresh);
  await act(() => result.current.review(true));
  expect(result.current.choices.token).toBeUndefined();
  expect(vi.mocked(api.reviewPlaybook).mock.calls.at(-1)?.[1].bindings).toEqual(
    [],
  );
});
test("a double create preparation admits only one request", async () => {
  const { result } = await setup();
  await act(() => result.current.review());
  await act(async () => {
    expect(
      await Promise.all([result.current.prepare(), result.current.prepare()]),
    ).toEqual([true, false]);
  });
  expect(api.confirmPlaybook).toHaveBeenCalledTimes(1);
  expect(api.createPlaybookTarget).toHaveBeenCalledTimes(1);
});
