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

const GO_LINE =
  "Built from this commit with the Go toolchain. No install scripts run; every download is verified against sum.golang.org.";
const MODULE = "git.b4mad.industries/agentic-forges/forgejo-mcp/v3";

test("a Go link's card (#2427): the Go facts, the computed dependencies, the build line last", async () => {
  vi.mocked(api.askProposalContent).mockResolvedValue({
    kind: "mcp_link",
    link: {
      repository: "codeberg.org/goern/forgejo-mcp",
      commit: "9c3fa38",
      manifest: "go.mod",
      kind: "go",
      package: MODULE,
      version: "v3.3.0",
      registry: "proxy.golang.org",
      record_field: "",
      names: "",
      integrity: "h1:a7TU1kvcAEAt",
      dependencies: 46,
      install_scripts: "",
      runs: "forgejo-mcp (stdio), built with go1.27.4",
      license: "",
      disclaimer: GO_LINE,
      home: "git.b4mad.industries/agentic-forges/forgejo-mcp",
      mirror: true,
      ref: "refs/tags/v3.3.0",
      go: "1.26.0",
      command: MODULE,
      entrypoint: "forgejo-mcp",
      toolchain: "1.27.4",
    },
    server: JSON.stringify({ name: "forgejo-mcp", source: { kind: "go", package: MODULE, version: "v3.3.0" } }, null, 2),
  });
  render(
    <MemoryRouter>
      <OperationCard p={{ ...card, title: "forgejo-mcp" }} deciding={false} error={null} onConfirm={vi.fn()} onDismiss={vi.fn()} />
    </MemoryRouter>,
  );
  const body = await screen.findByTestId("ask-mcp-link");
  expect(body).toHaveAttribute("data-kind", "go");
  expect(within(body).getByTestId("ask-mcp-link-source")).toHaveTextContent("codeberg.org/goern/forgejo-mcp @ 9c3fa38");
  expect(body).toHaveTextContent(`go.mod → Go module ${MODULE}`);
  expect(body).toHaveTextContent("same commit as the link (the link is a mirror)");
  expect(body).toHaveTextContent("v3.3.0 · the tag at this commit");
  expect(body).toHaveTextContent("forgejo-mcp · the module root");
  expect(body).toHaveTextContent("Go 1.26.0");
  expect(body).toHaveTextContent("h1:a7TU1kvcAEAt… · verified against sum.golang.org");
  expect(body).toHaveTextContent("46 modules in its build · each verified against sum.golang.org");
  expect(body).toHaveTextContent("go1.27.4 · BattleLab’s own, sha256-verified · cgo off");
  expect(body).not.toHaveTextContent("Installs the published release");
  // The label and its value are separate cells — "DEPENDENCIES46" was one run of text (#2427).
  const deps = within(body).getByText("Dependencies");
  expect(deps.tagName).toBe("DT");
  expect(deps.nextElementSibling).toHaveTextContent(/^46 modules/);
  const line = within(body).getByTestId("ask-mcp-disclaimer");
  expect(line).toHaveTextContent(GO_LINE);
  expect(body.lastElementChild).toBe(line);
});
