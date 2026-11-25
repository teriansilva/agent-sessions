/** HQ's create card for an MCP server from a repository link (#2353): the same facts as the wizard's
 *  Review, and the disclaimer as the last thing above Register. Confirm waits until they are shown. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { AskLibraryContent, AskOperationCard } from "../../types/api";
import { OperationCard } from "./AskCards";

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
  api: { askProposalContent: vi.fn() },
}));

const DISCLAIMER = "Installs the published release; not built from this commit.";
const CONTENT: AskLibraryContent = {
  kind: "mcp_link",
  link: {
    repository: "github.com/owner/repo",
    commit: "3f2a9c1",
    manifest: "package.json",
    kind: "npm",
    package: "@acme/mcp-server",
    version: "1.4.0",
    registry: "registry.npmjs.org",
    record_field: "repository",
    names: "github.com/owner/repo",
    integrity: "sha512-9fK2xQe7vB0m",
    dependencies: 23,
    install_scripts: "esbuild, sharp",
    runs: "node …/dist/index.js (stdio)",
    license: "MIT",
    disclaimer: DISCLAIMER,
  },
  server: JSON.stringify({ name: "acme", source: { kind: "npm", package: "@acme/mcp-server", version: "1.4.0" } }, null, 2),
};

const card: AskOperationCard = {
  action_id: "b".repeat(32),
  kind: "operation",
  op: "mcp.create",
  risk: "reversible",
  label: "Add MCP server",
  verb: "Register",
  title: "acme",
  text: "Registers the MCP server “acme”: the npm package @acme/mcp-server 1.4.0 that github.com/owner/repo declares at 3f2a9c1.",
  needs: [],
  state: "pending",
  summary: "",
  reason: "",
  can_confirm: true,
  can_dismiss: true,
  content: true,
};

beforeEach(() => {
  vi.mocked(api.askProposalContent).mockReset();
  vi.mocked(api.askProposalContent).mockResolvedValue(CONTENT);
});

test("the link card shows where the package comes from, the release, and the disclaimer last", async () => {
  render(
    <MemoryRouter>
      <OperationCard p={card} deciding={false} error={null} onConfirm={vi.fn()} onDismiss={vi.fn()} />
    </MemoryRouter>,
  );
  const body = await screen.findByTestId("ask-mcp-link");
  expect(within(body).getByTestId("ask-mcp-link-source")).toHaveTextContent("github.com/owner/repo @ 3f2a9c1");
  expect(body).toHaveTextContent("package.json → npm @acme/mcp-server 1.4.0");
  expect(body).toHaveTextContent("its repository names github.com/owner/repo, the link’s repository");
  expect(body).toHaveTextContent("sha512-9fK2xQe7vB0m…");
  expect(body).toHaveTextContent("esbuild, sharp declared · never run");
  const disclaimer = within(body).getByTestId("ask-mcp-disclaimer");
  expect(disclaimer).toHaveTextContent(DISCLAIMER);
  // The disclaimer is the content's last block: directly above the card's Register.
  expect(body.lastElementChild).toBe(disclaimer);
  expect(screen.getByRole("button", { name: "Register" })).toBeEnabled();
});
