/** Project → Playbook (#2143 P2, #1949): what the tab reads and when, which actions each state
 *  offers, and that resume (#2263), retry and refusals follow the server (#1191). The hook's own deploy/retry
 *  semantics are pinned in useProjectPlaybook.test.tsx; real-browser runs are
 *  e2e/projects-playbook.spec.ts and e2e/appmode-project-playbook-tab.spec.ts. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { ProjectEntity } from "../../types/api";
import type { DeploymentStatus } from "../../types/playbookDeployment";
import type { PlaybookDetail } from "../../types/playbooks";
import { ProjectPlaybookTab } from "./ProjectPlaybookTab";

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: Object.fromEntries(
      [
        "playbookDeployment",
        "verifyPlaybook",
        "playbook",
        "playbookProjects",
        "playbooks",
        "playbookAuthoring",
        "templateVariables",
        "reviewPlaybook",
        "confirmPlaybook",
        "bindPlaybook",
        "applyPlaybook",
        "reviewPlaybookRemoval",
        "removePlaybookDeployment",
        "resumePlaybookOperation",
      ].map((n) => [n, vi.fn()]),
    ),
  };
});
// The inputs step reads the engine roster; its own tests cover it.
vi.mock("../../components/playbooks/ProjectPlaybook", async (orig) => ({
  ...(await orig<typeof import("../../components/playbooks/ProjectPlaybook")>()),
  ProjectPlaybookInputs: () => <p>stub-inputs</p>,
}));

const ent = (over: Partial<ProjectEntity> = {}): ProjectEntity => ({
  id: "p-1",
  name: "SampleProject",
  color: "#5fd7ff",
  folders: ["/home/u/sample"],
  default_folder: "/home/u/sample",
  archived: false,
  created_at: 0,
  session_count: 0,
  ...over,
});
const detail = (over: Partial<PlaybookDetail> = {}): PlaybookDetail => ({
  id: "ship-it",
  name: "Ship it",
  ok: true,
  error: null,
  source: "bundled",
  editable: false,
  revision: "a".repeat(64),
  default: false,
  version: "1.3.0",
  recovery_total: 0,
  files: {},
  documents: {
    "playbook.toml": {
      format: 2,
      variables: [
        { name: "DEPLOY_HOST", type: "text" },
        { name: "SENTRY_DSN", kind: "secret", type: "text" },
      ],
    },
  },
  ...over,
});
const applied: DeploymentStatus = { project_id: "p-1", deployment_id: "d-1", playbook_id: "ship-it", state: "applied" };
const review = {
  digest: "b".repeat(64),
  deployment_id: "d-1",
  destination: { path: "/home/u/sample" },
  variables: [],
  materials: [],
  changes: [{ path: "RULES.md", action: "create", diff: "+rules" }],
  conflicts: [],
  targets: [],
  assignments: [],
  capability_requests: [],
  rituals: [],
};

beforeEach(() => {
  vi.resetAllMocks();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  vi.mocked(api.verifyPlaybook).mockResolvedValue({
    deployment_id: "d-1",
    ok: false,
    checks: {
      variables: { ok: false, problems: { SENTRY_DSN: "missing" } },
      materials: { ok: true, drift: [] },
      binaries: { ok: false, missing: ["docker"] },
      connections: { probed: false, targets: [{ id: "connection:x" }] },
    },
  } as never);
  vi.mocked(api.playbook).mockResolvedValue(detail());
  vi.mocked(api.playbookProjects).mockResolvedValue({
    playbook_id: "ship-it",
    revision: "c".repeat(64),
    projects: [{ project_id: "p-1", deployment_id: "d-1", state: "applied", revision: "a".repeat(64), update_available: true }],
  });
  vi.mocked(api.playbooks).mockResolvedValue({ playbooks: [detail()], default: null, recovery_total: 0 });
  vi.mocked(api.playbookAuthoring).mockResolvedValue({ agents: [] } as never);
  vi.mocked(api.templateVariables).mockResolvedValue({ variables: [] } as never);
  vi.mocked(api.reviewPlaybook).mockResolvedValue(review);
  vi.mocked(api.confirmPlaybook).mockResolvedValue({ receipt: "receipt", digest: review.digest });
  vi.mocked(api.bindPlaybook).mockResolvedValue({ receipt: "bound", digest: "d".repeat(64) });
  vi.mocked(api.applyPlaybook).mockResolvedValue({ project_id: "p-1", state: "applied" });
});

const show = (entity = ent()) =>
  render(
    <MemoryRouter>
      <ProjectPlaybookTab entity={entity} />
    </MemoryRouter>,
  );

test("an applied playbook verifies when the tab opens and again on Verify again — never otherwise", async () => {
  show();
  const block = await screen.findByTestId("playbook-verify");
  expect(api.verifyPlaybook).toHaveBeenCalledTimes(1);
  // A missing binary is a failure (red); a missing input needs a human (amber); a connection is idle.
  expect(within(block).getByText("Needs attention")).toBeTruthy();
  expect(block.dataset.tone).toBe("down");
  expect(within(block).getByText("missing: docker").closest("li")?.dataset.tone).toBe("down");
  expect(within(block).getByText("SENTRY_DSN: missing").closest("li")?.dataset.tone).toBe("att");
  expect(within(block).getByText("Not probed — 1 target listed").closest("li")?.dataset.tone).toBe("idle");
  // The update stamp names the NEW version (the fleet read says one exists).
  expect(screen.getByTestId("playbook-stamp").textContent).toBe("Update available → v1.3.0");
  // Inputs: the fleet says a newer revision waits, so the declarations are the CURRENT revision's
  // and verify checked the deployed one: only its problem is certain, nothing is drawn as Filled.
  expect(screen.getByTestId("playbook-inputs-revision")).toBeTruthy();
  expect(screen.getByTestId("playbook-input-SENTRY_DSN").textContent).toContain("Waiting");
  expect(screen.getByTestId("playbook-input-DEPLOY_HOST").textContent).toContain("Not verified");
  expect(screen.queryByText("Filled")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /Verify again/ }));
  await waitFor(() => expect(api.verifyPlaybook).toHaveBeenCalledTimes(2));
});

test("Review update reviews the deployed playbook's current revision on the existing project", async () => {
  show();
  await screen.findByTestId("playbook-verify");
  await userEvent.click(screen.getByRole("button", { name: /Review update/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalledTimes(1));
  const [id, inputs] = vi.mocked(api.reviewPlaybook).mock.calls[0];
  expect(id).toBe("ship-it");
  expect(inputs).toMatchObject({ project_id: "p-1", destination: "/home/u/sample", revision: "a".repeat(64) });
  expect(inputs).not.toHaveProperty("create");
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  await waitFor(() => expect(api.applyPlaybook).toHaveBeenCalledTimes(1));
  expect(api.bindPlaybook).toHaveBeenCalledWith("p-1", "ship-it", expect.objectContaining({ project_id: "p-1" }), "receipt", expect.any(String));
  expect(await screen.findByTestId("playbook-done")).toHaveProperty("textContent", "Updated and verified.");
});

test("a refused apply shows the server's words as they came", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "none" });
  vi.mocked(api.applyPlaybook).mockRejectedValue(new ApiError(409, "resolve material conflicts before applying"));
  show();
  await userEvent.click(await screen.findByRole("radio", { name: /Ship it/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", playbook_id: "ship-it", deployment_id: "d-1", state: "bound" });
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  expect((await screen.findByTestId("playbook-error")).textContent).toBe("resolve material conflicts before applying");
});

test("an empty project offers the library; an archived one offers no deploy action", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "none" });
  const { unmount } = show();
  expect(await screen.findByRole("radiogroup", { name: "Playbook" })).toBeTruthy();
  expect(api.verifyPlaybook).not.toHaveBeenCalled();
  unmount();
  show(ent({ archived: true }));
  expect(await screen.findByTestId("playbook-archived")).toBeTruthy();
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  expect(screen.queryByRole("button", { name: /Apply reviewed/ })).toBeNull();
});

test("an archived project's playbook can be verified and removed, never updated or changed", async () => {
  show(ent({ archived: true }));
  await screen.findByTestId("playbook-verify");
  expect(screen.getByTestId("playbook-archived")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Review update/ })).toBeNull();
  expect(screen.queryByTestId("playbook-change")).toBeNull();
  expect(screen.getByTestId("playbook-remove")).toBeTruthy();
  expect(screen.getByRole("button", { name: /Verify again/ })).toBeTruthy();
});

const interrupted = (operation: "bind" | "apply" | "remove", files?: DeploymentStatus["files"]): DeploymentStatus => ({
  project_id: "p-1",
  deployment_id: "d-1",
  playbook_id: "ship-it",
  state: "interrupted",
  operation,
  operation_id: "op-recorded",
  files,
});

test("an interrupted apply this page did not start resumes by its recorded id alone", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue(
    interrupted("apply", [
      { path: "RULES.md", state: "written" },
      { path: "docs/playbook.md", state: "not written" },
    ]),
  );
  show();
  const box = await screen.findByTestId("playbook-interrupted");
  // Amber: it needs you, nothing has failed.
  expect(box.dataset.tone).toBe("att");
  expect(within(box).getByText("op-recorded")).toBeTruthy();
  expect(within(box).getByText("not written")).toBeTruthy();
  // An interrupted deployment is never verified (the server refuses it).
  expect(api.verifyPlaybook).not.toHaveBeenCalled();
  // Removal settles an interrupted apply too, so it stays offered.
  expect(screen.getByTestId("playbook-remove")).toBeTruthy();
  expect(screen.queryByTestId("playbook-retry")).toBeNull();
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ project_id: "p-1", state: "applied" });
  const resume = screen.getByTestId("playbook-resume");
  expect(resume.textContent).toMatch(/Resume apply/);
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  await userEvent.click(resume);
  await waitFor(() => expect(api.resumePlaybookOperation).toHaveBeenCalledWith("p-1", "apply", "op-recorded"));
  expect(await screen.findByTestId("playbook-done")).toHaveTextContent("Applied.");
  await screen.findByTestId("playbook-verify");
  // Only the id was sent: no review, bind or apply request.
  expect(api.reviewPlaybook).not.toHaveBeenCalled();
  expect(api.bindPlaybook).not.toHaveBeenCalled();
  expect(api.applyPlaybook).not.toHaveBeenCalled();
});

test("the page that started an apply resumes it by id too, and never resends its held request", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "none" });
  vi.mocked(api.applyPlaybook).mockRejectedValueOnce(new ApiError(503, "Service unavailable"));
  show();
  await userEvent.click(await screen.findByRole("radio", { name: /Ship it/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  // The failed apply left an interrupted journal under the id this page sent.
  vi.mocked(api.playbookDeployment).mockImplementation(async () => ({
    ...interrupted("apply"),
    operation_id: vi.mocked(api.applyPlaybook).mock.calls[0][2],
  }));
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  const resume = await screen.findByTestId("playbook-resume");
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ project_id: "p-1", state: "applied" });
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  await userEvent.click(resume);
  await waitFor(() =>
    expect(api.resumePlaybookOperation).toHaveBeenCalledWith("p-1", "apply", vi.mocked(api.applyPlaybook).mock.calls[0][2]),
  );
  await screen.findByTestId("playbook-verify");
  expect(api.applyPlaybook).toHaveBeenCalledTimes(1);
  expect(api.bindPlaybook).toHaveBeenCalledTimes(1);
  // The original failure is settled: its alert goes, and the completion shows.
  expect(await screen.findByTestId("playbook-done")).toHaveTextContent("Applied.");
  expect(screen.queryByTestId("playbook-error")).toBeNull();
});

test("Resume binding stops at bound; applying is its own step with the resumed bind's review", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue(interrupted("bind"));
  show();
  const resume = await screen.findByTestId("playbook-resume");
  expect(resume.textContent).toMatch(/Resume binding/);
  // A pending bind can't be removed (the server refuses it).
  expect(screen.queryByTestId("playbook-remove")).toBeNull();
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ receipt: "resumed-receipt", digest: "d".repeat(64) });
  vi.mocked(api.playbookDeployment).mockResolvedValue({ ...applied, state: "bound" });
  await userEvent.click(resume);
  await waitFor(() => expect(api.resumePlaybookOperation).toHaveBeenCalledWith("p-1", "bind", "op-recorded"));
  expect(await screen.findByTestId("playbook-done")).toHaveTextContent("Nothing is written until you apply.");
  expect(api.applyPlaybook).not.toHaveBeenCalled();
  const go = await screen.findByTestId("playbook-apply-bound");
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  await userEvent.click(go);
  await waitFor(() => expect(api.applyPlaybook).toHaveBeenCalledTimes(1));
  const [project, receipt, op] = vi.mocked(api.applyPlaybook).mock.calls[0];
  expect([project, receipt]).toEqual(["p-1", "resumed-receipt"]);
  expect(op).toMatch(/^[a-z0-9][a-z0-9-]{7,63}$/);
  await screen.findByTestId("playbook-verify");
});

test("an expired review after a resumed bind is refused verbatim, then reviewed afresh", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue(interrupted("bind"));
  show();
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ receipt: "old-receipt", digest: "d".repeat(64) });
  vi.mocked(api.playbookDeployment).mockResolvedValue({ ...applied, state: "bound" });
  await userEvent.click(await screen.findByTestId("playbook-resume"));
  vi.mocked(api.applyPlaybook).mockRejectedValueOnce(new ApiError(422, "the review expired; review it again"));
  await userEvent.click(await screen.findByTestId("playbook-apply-bound"));
  const refused = await screen.findByTestId("playbook-resume-refused");
  expect(refused).toHaveTextContent("the review expired; review it again");
  expect(refused).toHaveTextContent("Review the changes again to apply them.");
  expect(refused.dataset.tone).toBe("att");
  // The expired receipt is never sent again: the way on is a fresh review of the same playbook.
  expect(screen.queryByTestId("playbook-apply-bound")).toBeNull();
  await userEvent.click(screen.getByTestId("playbook-review-again"));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  expect(vi.mocked(api.reviewPlaybook).mock.calls[0][0]).toBe("ship-it");
});

test("a bound deployment with nothing held offers a fresh review, never an apply it can't authorize", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue({ ...applied, state: "bound" });
  show();
  expect(await screen.findByTestId("playbook-bound")).toHaveTextContent("Review the changes to apply them");
  expect(screen.getByTestId("playbook-review-again")).toBeTruthy();
  expect(screen.queryByTestId("playbook-apply-bound")).toBeNull();
  expect(screen.queryByTestId("playbook-retry")).toBeNull();
});

test("Resume removal finishes the recorded removal and the library returns", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue(
    interrupted("remove", [
      { path: "RULES.md", state: "removed" },
      { path: "docs/playbook.md", state: "not removed" },
    ]),
  );
  show();
  const resume = await screen.findByTestId("playbook-resume");
  expect(resume.textContent).toMatch(/Resume removal/);
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ project_id: "p-1", state: "removed" });
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "removed" });
  await userEvent.click(resume);
  await waitFor(() => expect(api.resumePlaybookOperation).toHaveBeenCalledWith("p-1", "remove", "op-recorded"));
  expect(await screen.findByTestId("playbook-done")).toHaveTextContent("Removed. The project and its folders stay.");
  expect(await screen.findByRole("radiogroup", { name: "Playbook" })).toBeTruthy();
  expect(api.reviewPlaybookRemoval).not.toHaveBeenCalled();
  expect(api.removePlaybookDeployment).not.toHaveBeenCalled();
});

test("a refused resume shows the server's words, in amber, with the way on", async () => {
  vi.mocked(api.playbookDeployment).mockResolvedValue(interrupted("apply"));
  vi.mocked(api.resumePlaybookOperation).mockRejectedValue(
    new ApiError(409, "the accepted apply plan no longer reproduces"),
  );
  show();
  await userEvent.click(await screen.findByTestId("playbook-resume"));
  const refused = await screen.findByTestId("playbook-resume-refused");
  expect(refused).toHaveTextContent("the accepted apply plan no longer reproduces");
  expect(refused).toHaveTextContent("Remove playbook… settles this apply");
  expect(refused.dataset.tone).toBe("att");
  // Still interrupted, so Resume and Remove both stay.
  expect(screen.getByTestId("playbook-resume")).toBeTruthy();
  expect(screen.getByTestId("playbook-remove")).toBeTruthy();
});

test("a failed status read is a red state with Retry, never an empty playbook", async () => {
  vi.mocked(api.playbookDeployment).mockRejectedValueOnce(new ApiError(503, "Service unavailable"));
  show();
  const failed = await screen.findByTestId("playbook-read-failed");
  expect(failed.textContent).toContain("Service unavailable (HTTP 503). Nothing was changed.");
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  await userEvent.click(within(failed).getByRole("button", { name: /Retry/ }));
  expect(await screen.findByTestId("playbook-verify")).toBeTruthy();
});

const current = (revision = "a".repeat(64)) =>
  vi.mocked(api.playbookProjects).mockResolvedValue({
    playbook_id: "ship-it",
    revision,
    projects: [{ project_id: "p-1", deployment_id: "d-1", state: "applied", revision: "a".repeat(64), update_available: revision !== "a".repeat(64) }],
  });

test("inputs are Filled only when verify checked the same revision they are declared by", async () => {
  current();
  show();
  await screen.findByTestId("playbook-verify");
  expect(screen.getByRole("heading", { name: /Inputs.*1 of 2 filled/ })).toBeTruthy();
  expect(screen.getByTestId("playbook-input-DEPLOY_HOST").textContent).toContain("Filled");
  expect(screen.getByTestId("playbook-input-SENTRY_DSN").textContent).toContain("Waiting");
  expect(screen.queryByTestId("playbook-inputs-revision")).toBeNull();
});

test("a newer revision's added input is never Filled; a removed input verify still fails on stays listed", async () => {
  // The current revision adds NEW_INPUT and drops SENTRY_DSN; the deployed one still fails on it.
  vi.mocked(api.playbook).mockResolvedValue(
    detail({
      revision: "c".repeat(64),
      documents: { "playbook.toml": { format: 2, variables: [{ name: "DEPLOY_HOST", type: "text" }, { name: "NEW_INPUT", type: "text" }] } },
    }),
  );
  show();
  await screen.findByTestId("playbook-verify");
  expect(screen.getByTestId("playbook-input-NEW_INPUT").textContent).toContain("Not verified");
  expect(screen.getByTestId("playbook-input-DEPLOY_HOST").textContent).toContain("Not verified");
  const gone = screen.getByTestId("playbook-input-SENTRY_DSN");
  expect(gone.textContent).toContain("Waiting");
  expect(gone.textContent).toContain("missing");
  expect(screen.queryByText("Filled")).toBeNull();
});

/** The apply fails with a lost response and the server records it as interrupted under the id sent. */
function interruptNextApply() {
  vi.mocked(api.applyPlaybook).mockRejectedValueOnce(new ApiError(503, "Service unavailable"));
  vi.mocked(api.playbookDeployment).mockImplementation(async () =>
    vi.mocked(api.applyPlaybook).mock.calls.length
      ? {
          project_id: "p-1",
          deployment_id: "d-1",
          playbook_id: "ship-it",
          state: "interrupted",
          operation: "apply",
          operation_id: vi.mocked(api.applyPlaybook).mock.calls[0][2],
        }
      : applied,
  );
}

test("an interrupted UPDATE offers Resume: no review or choice is offered until it settles", async () => {
  show();
  await screen.findByTestId("playbook-verify");
  await userEvent.click(screen.getByRole("button", { name: /Review update/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  interruptNextApply();
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  const resume = await screen.findByTestId("playbook-resume");
  // Nothing that would start a second operation is on screen.
  expect(screen.queryByRole("button", { name: /Refresh review|Review playbook changes/ })).toBeNull();
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ project_id: "p-1", state: "applied" });
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  await userEvent.click(resume);
  await waitFor(() =>
    expect(api.resumePlaybookOperation).toHaveBeenCalledWith("p-1", "apply", vi.mocked(api.applyPlaybook).mock.calls[0][2]),
  );
  expect(api.applyPlaybook).toHaveBeenCalledTimes(1);
  expect(api.reviewPlaybook).toHaveBeenCalledTimes(1);
});

test("Change → a replacement whose apply is interrupted shows the recorded operation and Resume, not the library", async () => {
  vi.mocked(api.reviewPlaybookRemoval).mockResolvedValue({
    digest: "e".repeat(64),
    changes: [{ path: "RULES.md", action: "remove" }],
    conflicts: [],
    bindings: { remove: [], restore: [], refusal: null },
    directories: [],
    secret_files: [],
  });
  vi.mocked(api.removePlaybookDeployment).mockResolvedValue({ project_id: "p-1", state: "removed" });
  show();
  await screen.findByTestId("playbook-verify");
  await userEvent.click(screen.getByTestId("playbook-change"));
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "removed" });
  await userEvent.click(await screen.findByRole("button", { name: "Remove reviewed playbook" }));
  await userEvent.click(await screen.findByRole("radio", { name: /Ship it/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  interruptNextApply();
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  expect(await screen.findByTestId("playbook-interrupted")).toBeTruthy();
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  // Resuming lands on the replacement as applied, never back in the library.
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ project_id: "p-1", state: "applied" });
  vi.mocked(api.playbookDeployment).mockResolvedValue(applied);
  await userEvent.click(screen.getByTestId("playbook-resume"));
  await screen.findByTestId("playbook-verify");
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  expect(screen.getByTestId("playbook-remove")).toBeTruthy();
  expect(await screen.findByTestId("playbook-done")).toHaveTextContent("Applied.");
  expect(screen.queryByTestId("playbook-error")).toBeNull();
});

test("Change → a replacement whose bind is interrupted resumes to bound, with its separate Apply", async () => {
  vi.mocked(api.reviewPlaybookRemoval).mockResolvedValue({
    digest: "e".repeat(64),
    changes: [{ path: "RULES.md", action: "remove" }],
    conflicts: [],
    bindings: { remove: [], restore: [], refusal: null },
    directories: [],
    secret_files: [],
  });
  vi.mocked(api.removePlaybookDeployment).mockResolvedValue({ project_id: "p-1", state: "removed" });
  show();
  await screen.findByTestId("playbook-verify");
  await userEvent.click(screen.getByTestId("playbook-change"));
  vi.mocked(api.playbookDeployment).mockResolvedValue({ project_id: "p-1", state: "removed" });
  await userEvent.click(await screen.findByRole("button", { name: "Remove reviewed playbook" }));
  await userEvent.click(await screen.findByRole("radio", { name: /Ship it/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Review playbook changes" }));
  await waitFor(() => expect(api.reviewPlaybook).toHaveBeenCalled());
  vi.mocked(api.bindPlaybook).mockRejectedValueOnce(new ApiError(503, "Service unavailable"));
  vi.mocked(api.playbookDeployment).mockImplementation(async () => ({
    ...interrupted("bind"),
    operation_id: vi.mocked(api.bindPlaybook).mock.calls[0]?.[4] ?? "op-recorded",
  }));
  await userEvent.click(screen.getByRole("button", { name: "Apply reviewed changes to this project" }));
  const resume = await screen.findByTestId("playbook-resume");
  vi.mocked(api.resumePlaybookOperation).mockResolvedValue({ receipt: "resumed-receipt", digest: "d".repeat(64) });
  vi.mocked(api.playbookDeployment).mockResolvedValue({ ...applied, state: "bound" });
  await userEvent.click(resume);
  expect(await screen.findByTestId("playbook-apply-bound")).toBeTruthy();
  expect(screen.queryByRole("radiogroup", { name: "Playbook" })).toBeNull();
  expect(screen.queryByTestId("playbook-error")).toBeNull();
  expect(api.applyPlaybook).not.toHaveBeenCalled();
});
