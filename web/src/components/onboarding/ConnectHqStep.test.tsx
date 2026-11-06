import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import {
  tileInstance,
  tileLine,
  type ConnectorCatalog,
  type ConnectorInstance,
  type ConnectorManifest,
} from "../../lib/connectors";
import { ConnectHqStep } from "./ConnectHqStep";

vi.mock("../../lib/api", () => ({
  api: {
    connectors: vi.fn(),
    addConnector: vi.fn(),
    removeConnector: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

const GITHUB: ConnectorManifest = {
  id: "github",
  label: "GitHub",
  publisher: "battlelab",
  version: "1",
  category: "versioning",
  adapter: "github",
  origin_mode: "fixed",
  fixed_origin: "https://api.github.com",
  auth: { kind: "token", scopes: [] },
  capabilities: { read: ["issues", "pulls", "ci"] },
  setup: "",
};
const TELEGRAM: ConnectorManifest = {
  ...GITHUB,
  id: "telegram",
  label: "Telegram",
  category: "messenger",
  adapter: "telegram",
  capabilities: { read: [], send: ["message"] },
};

function inst(over: Partial<ConnectorInstance>): ConnectorInstance {
  return {
    id: "c1",
    manifest_id: "github",
    origin: "https://api.github.com",
    status: "ready",
    token_set: true,
    account: null,
    generation: 1,
    tested_generation: 1,
    tested: { login: "octo", repos: 3, at: 1 },
    last_test: { generation: 1, ok: true, code: null, at: 1 },
    enabled: { read: true },
    ...over,
  };
}

function catalog(instances: ConnectorInstance[]): ConnectorCatalog {
  return { catalog: [GITHUB, TELEGRAM], instances, disclosure: "" };
}

beforeEach(() => {
  vi.mocked(api.connectors).mockReset();
  vi.mocked(api.addConnector).mockReset();
});

const tile = (id: string) =>
  screen
    .getAllByTestId("connect-hq-tile")
    .find((el) => el.getAttribute("data-connector") === id)!;

test("the tile line comes from the manifest's capabilities", () => {
  expect(tileLine(GITHUB)).toBe("HQ reads your issues, pull requests and CI status.");
  expect(
    tileLine({ ...GITHUB, capabilities: { read: ["folders", "search", "documents"] } }),
  ).toBe("HQ reads your folders and text files.");
  expect(tileLine({ ...GITHUB, capabilities: { read: ["files"] } })).toBe("HQ reads your files.");
  expect(tileLine(TELEGRAM)).toMatch(/nothing is sent until you tap it/);
});

test("a tile stands for the instance HQ works through, else the first", () => {
  const off = inst({ id: "a", enabled: { read: false } });
  const on = inst({ id: "b" });
  expect(tileInstance(GITHUB, [off, on])?.id).toBe("b");
  expect(tileInstance(GITHUB, [off])?.id).toBe("a");
  expect(tileInstance(TELEGRAM, [off, on])).toBeUndefined();
});

test("tiles show Settings' own state words: not set up, connected, failed", async () => {
  vi.mocked(api.connectors).mockResolvedValue(
    catalog([
      inst({
        id: "g",
        last_test: { generation: 1, ok: false, code: "auth", at: 1 },
        status: "needs_test",
      }),
    ]),
  );
  render(<ConnectHqStep />);
  await waitFor(() => expect(screen.getAllByTestId("connect-hq-tile")).toHaveLength(2));
  expect(within(tile("telegram")).getByTestId("connect-hq-state")).toHaveTextContent(
    "Not set up",
  );
  expect(within(tile("telegram")).getByRole("button")).toHaveTextContent("Set up Telegram");
  expect(within(tile("github")).getByTestId("connect-hq-state")).toHaveTextContent(
    "Test failed",
  );
  expect(within(tile("github")).getByRole("button")).toHaveTextContent("Fix GitHub");
});

test("a connected tile offers Manage", async () => {
  vi.mocked(api.connectors).mockResolvedValue(catalog([inst({})]));
  render(<ConnectHqStep />);
  await waitFor(() =>
    expect(within(tile("github")).getByTestId("connect-hq-state")).toHaveTextContent(
      "HQ reads through it",
    ),
  );
  expect(within(tile("github")).getByRole("button")).toHaveTextContent("Manage GitHub");
});

test("Set up adds the connector and opens Settings' card in place; Close folds it back", async () => {
  const added = inst({
    id: "new",
    status: "needs_token",
    token_set: false,
    tested: null,
    tested_generation: null,
    last_test: null,
    enabled: { read: false },
  });
  vi.mocked(api.connectors)
    .mockResolvedValueOnce(catalog([]))
    .mockResolvedValue(catalog([added]));
  vi.mocked(api.addConnector).mockResolvedValue(added);
  render(<ConnectHqStep />);
  await userEvent.click(await screen.findByRole("button", { name: "Set up GitHub" }));
  expect(api.addConnector).toHaveBeenCalledWith("github");
  const card = await screen.findByTestId("connector-card");
  expect(within(card).getByTestId("connector-status")).toHaveTextContent("Needs a token");
  await userEvent.click(within(card).getByRole("button", { name: "Close" }));
  expect(screen.queryByTestId("connector-card")).toBeNull();
  expect(within(tile("github")).getByRole("button")).toHaveTextContent("Manage GitHub");
  // Opening an existing one adds nothing.
  await userEvent.click(within(tile("github")).getByRole("button"));
  expect(await screen.findByTestId("connector-card")).toBeInTheDocument();
  expect(api.addConnector).toHaveBeenCalledTimes(1);
});

test("an open card whose connector was removed folds back to its tile", async () => {
  vi.mocked(api.connectors)
    .mockResolvedValueOnce(catalog([inst({})]))
    .mockResolvedValue(catalog([]));
  vi.mocked(api.removeConnector).mockResolvedValue(undefined as never);
  render(<ConnectHqStep />);
  await userEvent.click(await screen.findByRole("button", { name: "Manage GitHub" }));
  const card = await screen.findByTestId("connector-card");
  await userEvent.click(within(card).getByRole("button", { name: "Remove" }));
  await userEvent.click(within(card).getByRole("button", { name: "Remove connector" }));
  await waitFor(() => expect(screen.queryByTestId("connector-card")).toBeNull());
  expect(within(tile("github")).getByTestId("connect-hq-state")).toHaveTextContent(
    "Not set up",
  );
});

test("a failed add says so and opens nothing", async () => {
  vi.mocked(api.connectors).mockResolvedValue(catalog([]));
  vi.mocked(api.addConnector).mockRejectedValue(new Error("catalog closed"));
  render(<ConnectHqStep />);
  await userEvent.click(await screen.findByRole("button", { name: "Set up GitHub" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("catalog closed");
  expect(screen.queryByTestId("connector-card")).toBeNull();
});

test("the added connector's card opens before the re-read that lists it lands", async () => {
  // The browser found it (#1880): with a real network the re-read after an add is slower than the
  // render, and a card waiting for it was folded back as "removed" before it ever showed.
  const added = inst({
    id: "slow",
    status: "needs_token",
    token_set: false,
    tested: null,
    tested_generation: null,
    last_test: null,
    enabled: { read: false },
  });
  let release: (c: ConnectorCatalog) => void = () => {};
  vi.mocked(api.connectors)
    .mockResolvedValueOnce(catalog([]))
    .mockReturnValueOnce(new Promise((r) => (release = r)));
  vi.mocked(api.addConnector).mockResolvedValue(added);
  render(<ConnectHqStep />);
  await userEvent.click(await screen.findByRole("button", { name: "Set up GitHub" }));
  const card = await screen.findByTestId("connector-card");
  expect(within(card).getByTestId("connector-status")).toHaveTextContent("Needs a token");
  release(catalog([added]));
  await waitFor(() => expect(api.connectors).toHaveBeenCalledTimes(2));
  expect(screen.getByTestId("connector-card")).toBeInTheDocument();
});

test("several accounts of one connector: the tile shows the working one and says there are more", async () => {
  vi.mocked(api.connectors).mockResolvedValue(
    catalog([inst({ id: "off", enabled: { read: false } }), inst({ id: "on" })]),
  );
  render(<ConnectHqStep />);
  await waitFor(() =>
    expect(within(tile("github")).getByTestId("connect-hq-state")).toHaveTextContent(
      "HQ reads through it",
    ),
  );
  expect(within(tile("github")).getByTestId("connect-hq-more")).toHaveTextContent("2 set up");
  await userEvent.click(within(tile("github")).getByRole("button", { name: "Manage GitHub" }));
  expect(await screen.findByTestId("connector-card")).toBeInTheDocument();
  expect(api.addConnector).not.toHaveBeenCalled();
  expect(screen.queryByTestId("connect-hq-more")).toBeNull();
});

test("a tested connector with reading off is not shown as connected", async () => {
  vi.mocked(api.connectors).mockResolvedValue(catalog([inst({ enabled: { read: false } })]));
  render(<ConnectHqStep />);
  await waitFor(() =>
    expect(within(tile("github")).getByTestId("connect-hq-state")).toHaveTextContent(
      "Tested · reading is off",
    ),
  );
});
