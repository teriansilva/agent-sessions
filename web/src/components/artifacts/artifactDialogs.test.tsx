/** The artifacts dialogs' logic (#1932 P2): the share confirm stays off until "I checked the files",
 *  a 409 `public_off` hands over to the "public links are off" dialog, and the activation dialog
 *  says the right restart sentence for the change it made — and offers no Restart button. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { resetRoster, setRoster } from "../../app/engineRoster";
import { ApiError } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import type { ArtifactActivation, ArtifactDetail } from "../../types/artifacts";
import { SessionActivationDialog } from "./ActivationDialog";
import { SharePubliclyDialog } from "./ShareDialogs";

vi.mock("../../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...real,
    api: {
      shareArtifact: vi.fn(),
      artifactActivation: vi.fn(),
      setArtifactActivation: vi.fn(),
      session: vi.fn(),
    },
  };
});
const { api } = await import("../../lib/api");

const ROSTER = fixture.engines as unknown as EngineInfo[];
/** A fixture agent that declares the tool channel, and one that does not — from the data. */
const WITH = ROSTER.find(
  (e) => e.kind === "agent" && typeof e.tools?.mcp === "string" && e.tools.mcp,
)!;
const WITHOUT = {
  ...WITH,
  id: "zeta",
  tools: { mcp: null },
  display: { ...WITH.display, name: "Zeta" },
} as EngineInfo;

const A: ArtifactDetail = {
  id: "5449-5592-2107-4372",
  title: "Ingest rate-limit report",
  why: "Load test",
  kind: "site",
  revision: 4,
  created_at: 0,
  updated_at: 0,
  origin: {
    engine: WITH.id,
    session_key: `${WITH.id}:s1`,
    cwd: "/w",
    project_id: null,
    mission_id: null,
  },
  visibility: "private",
  url: "/artifacts/5449-5592-2107-4372/",
  public_url: null,
  public_since: null,
  revisions: [],
  entry: "index.html",
};

beforeEach(() => setRoster([...ROSTER, WITHOUT]));
afterEach(() => {
  resetRoster();
  vi.clearAllMocks();
});

test("Share publicly stays off until the files are checked, then shares once", async () => {
  const onShared = vi.fn();
  vi.mocked(api.shareArtifact).mockResolvedValue({
    ...A,
    files: 38,
    bytes: 2_516_582,
    visibility: "public",
    public_url: "/artifacts/p/x/",
  });
  render(
    <MemoryRouter>
      <SharePubliclyDialog
        artifact={A}
        files={38}
        bytes={2_516_582}
        onCancel={() => {}}
        onShared={onShared}
        onPublicOff={() => {}}
      />
    </MemoryRouter>,
  );
  const confirm = screen.getByTestId("artifact-share-confirm");
  expect(confirm).toBeDisabled();
  expect(
    screen.getByText(/I checked the files: 38 files, 2.4 MB/),
  ).toBeInTheDocument();
  await userEvent.click(confirm);
  expect(api.shareArtifact).not.toHaveBeenCalled();
  await userEvent.click(screen.getByTestId("artifact-share-checked"));
  expect(confirm).toBeEnabled();
  await userEvent.click(confirm);
  await waitFor(() => expect(onShared).toHaveBeenCalledTimes(1));
  expect(api.shareArtifact).toHaveBeenCalledWith(A.id);
});

test("the server's 409 public_off hands over to the off dialog with its reason", async () => {
  const onPublicOff = vi.fn();
  vi.mocked(api.shareArtifact).mockRejectedValue(
    new ApiError(409, "public_off", {
      detail: "public_off",
      reason: "no login here",
    }),
  );
  render(
    <MemoryRouter>
      <SharePubliclyDialog
        artifact={A}
        files={1}
        bytes={10}
        onCancel={() => {}}
        onShared={() => {}}
        onPublicOff={onPublicOff}
      />
    </MemoryRouter>,
  );
  await userEvent.click(screen.getByTestId("artifact-share-checked"));
  await userEvent.click(screen.getByTestId("artifact-share-confirm"));
  await waitFor(() =>
    expect(onPublicOff).toHaveBeenCalledWith("no login here"),
  );
});

function activation(
  mode: ArtifactActivation["mode"],
  effective: "off" | "private" | "public",
): ArtifactActivation {
  return {
    scope: "session",
    id: `${WITH.id}:s1`,
    mode,
    enabled: true,
    effective: { mode: effective, source: mode ? "session" : "mission" },
  };
}

test("Off → Private on a running agent: 'does not have the tool yet'; no Restart button", async () => {
  vi.mocked(api.artifactActivation)
    .mockResolvedValueOnce(activation(null, "off"))
    .mockResolvedValueOnce(activation("private", "private"));
  vi.mocked(api.setArtifactActivation).mockResolvedValue({
    scope: "session",
    id: "x",
    mode: "private",
  });
  render(
    <MemoryRouter>
      <SessionActivationDialog
        session={{
          id: `${WITH.id}:s1`,
          engine: WITH.id,
          cwd: "/w",
          running: true,
          title: "t",
        }}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  const priv = await screen.findByRole("radio", { name: "Private" });
  expect(screen.getByRole("radio", { name: "Off" })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await userEvent.click(priv);
  expect(api.setArtifactActivation).toHaveBeenCalledWith(
    "session",
    `${WITH.id}:s1`,
    "private",
  );
  const notice = await screen.findByTestId("artifact-restart-notice");
  expect(notice).toHaveTextContent("Takes effect when the agent restarts.");
  expect(notice).toHaveTextContent(
    `The running ${WITH.id} does not have the tool yet; it gets it at restart.`,
  );
  expect(screen.queryByRole("button", { name: /restart now/i })).toBeNull();
});

test("Private → Public: 'the new mode applies when the agent restarts'", async () => {
  vi.mocked(api.artifactActivation)
    .mockResolvedValueOnce(activation("private", "private"))
    .mockResolvedValueOnce(activation("public", "public"));
  vi.mocked(api.setArtifactActivation).mockResolvedValue({
    scope: "session",
    id: "x",
    mode: "public",
  });
  render(
    <MemoryRouter>
      <SessionActivationDialog
        session={{
          id: `${WITH.id}:s1`,
          engine: WITH.id,
          cwd: "/w",
          running: true,
          title: "t",
        }}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("radio", { name: "Public" }));
  const notice = await screen.findByTestId("artifact-restart-notice");
  expect(notice).toHaveTextContent(
    `The new mode applies when the ${WITH.id} restarts.`,
  );
  expect(notice).not.toHaveTextContent("does not have the tool yet");
});

test("a stopped session gets no restart notice: its next launch takes the mode", async () => {
  vi.mocked(api.artifactActivation)
    .mockResolvedValueOnce(activation(null, "off"))
    .mockResolvedValueOnce(activation("private", "private"));
  vi.mocked(api.setArtifactActivation).mockResolvedValue({
    scope: "session",
    id: "x",
    mode: "private",
  });
  render(
    <MemoryRouter>
      <SessionActivationDialog
        session={{
          id: `${WITH.id}:s1`,
          engine: WITH.id,
          cwd: "/w",
          running: false,
          title: "t",
        }}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("radio", { name: "Private" }));
  await waitFor(() => expect(api.artifactActivation).toHaveBeenCalledTimes(2));
  expect(screen.queryByTestId("artifact-restart-notice")).toBeNull();
});

test("an agent without a tool channel shows the control disabled, with the reason, and asks nothing", () => {
  render(
    <MemoryRouter>
      <SessionActivationDialog
        session={{ id: "zeta:s9", engine: "zeta", cwd: "/w", running: true }}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("artifact-not-available")).toHaveTextContent(
    "Not available for this agent.",
  );
  for (const name of ["Off", "Private", "Public"])
    expect(screen.getByRole("radio", { name })).toBeDisabled();
});

test("a mission change names only the running agents whose next launch changes (board 4 e)", async () => {
  const a = `${WITH.id}:a`; // inherits from the mission: Off → Public
  const b = `${WITH.id}:b`; // its own Private wins over the mission: unchanged
  const c = `${WITH.id}:c`; // changes too, but is not running: its next launch simply takes it
  const sessions: Record<string, { title: string; running: boolean }> = {
    [a]: { title: "Migrate billing", running: true },
    [b]: { title: "Rate-limit", running: true },
    [c]: { title: "Stopped one", running: false },
  };
  let missionMode: "public" | null = null;
  vi.mocked(api.session).mockImplementation(
    async (key: string) =>
      ({ id: key, engine: WITH.id, cwd: "/w", ...sessions[key] }) as never,
  );
  vi.mocked(api.artifactActivation).mockImplementation(async (scope, id) => {
    if (scope === "mission")
      return { scope, id, mode: missionMode, enabled: true };
    const mode = id === b ? "private" : (missionMode ?? "off");
    return {
      scope,
      id,
      mode: null,
      enabled: true,
      effective: { mode, source: id === b ? "session" : "mission" },
    };
  });
  vi.mocked(api.setArtifactActivation).mockImplementation(
    async (scope, id, mode) => {
      missionMode = mode as "public" | null;
      return { scope, id, mode };
    },
  );
  const { MissionActivationDialog } = await import("./ActivationDialog");
  render(
    <MemoryRouter>
      <MissionActivationDialog
        mission={{
          id: "msn_1",
          title: "Harden the public ingest API",
          cwd: "/w",
          sessions: [a, b, c].map((session_key) => ({ session_key })),
        }}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("radio", { name: "Public" }));
  const notice = await screen.findByTestId("artifact-restart-notice");
  expect(notice).toHaveTextContent(
    `Migrate billing: The running ${WITH.id} does not have the tool yet; it gets it at restart.`,
  );
  expect(notice).not.toHaveTextContent("Rate-limit");
  expect(notice).not.toHaveTextContent("Stopped one");
});
