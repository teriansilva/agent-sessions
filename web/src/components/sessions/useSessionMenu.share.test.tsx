import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { resetRoster } from "../../app/engineRoster";
import type { Session } from "../../types/api";
import type { RowMenuEntry, RowMenuItem } from "../sidebar/RowMenu";
import { SHARE_PUBLIC_UNAVAILABLE } from "./shareAction";
import { type SessionMenuHandlers, useSessionMenu } from "./useSessionMenu";

/** The session ⋯ menu's Share group and Adopt entry, per the runtime the ROSTER gives the
 *  session's engine (#2173). The fixture roster: `claude` is a terminal client (`pty`),
 *  `claude-api` an API one, `apichat` a chat pane. */

vi.mock("../shares/SharePubliclyModal", () => ({
  SharePubliclyModal: ({ sessionKey }: { sessionKey: string }) => (
    <div role="dialog" aria-label="Share publicly" data-key={sessionKey} />
  ),
}));
vi.mock("../federation/SharePeerModal", () => ({
  SharePeerModal: ({ sessionKey, kind }: { sessionKey: string; kind?: string }) => (
    <div role="dialog" aria-label="Share with a peer" data-key={sessionKey} data-kind={kind} />
  ),
}));

const handlers: SessionMenuHandlers = {
  onToggleArchive: async () => {},
  onToggleFavorite: async () => {},
  onToggleOrchestratorExcluded: async () => {},
  onSetProject: async () => {},
  onEdit: () => {},
};

function sess(engine: string, over: Partial<Session> = {}): Session {
  const id = `${engine}:11111111-1111-4111-8111-111111111111`;
  return {
    id,
    engine,
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/claude",
    project: { kind: "folder" as const, id: "/home/m/claude", name: "/home/m/claude" },
    last_mtime: 1,
    first_user_message: "",
    title: "A session",
    sticky: false,
    archived: false,
    mission: null,
    ...over,
  };
}

let seen: RowMenuEntry[] = [];
function Host({ s, omit }: { s: Session; omit?: ReadonlySet<string> }) {
  const menu = useSessionMenu(s, handlers, { omit });
  const { items } = menu;
  useEffect(() => {
    seen = items;
  });
  return (
    <>
      {menu.items.map((it) =>
        typeof it === "string" || "group" in it ? null : (
          <button key={it.key} type="button" aria-disabled={it.disabled} onClick={it.onSelect}>
            {it.key}
          </button>
        ),
      )}
      <output data-testid="dialog-open">{String(menu.dialogOpen)}</output>
      {menu.dialogs}
    </>
  );
}
const mount = (s: Session, omit?: ReadonlySet<string>) =>
  render(
    <MemoryRouter>
      <Host s={s} omit={omit} />
    </MemoryRouter>,
  );

const item = (key: string) =>
  seen.find((e): e is RowMenuItem => typeof e !== "string" && "key" in e && e.key === key);
const groups = () =>
  seen.filter((e): e is { group: string } => typeof e !== "string" && "group" in e);
/** No leading, trailing or doubled rule, and never a rule straight after a group label's items
 *  were omitted. */
function rulesAreClean() {
  expect(seen[0]).not.toBe("separator");
  expect(seen[seen.length - 1]).not.toBe("separator");
  seen.forEach((e, i) => {
    if (e === "separator") expect(seen[i + 1]).not.toBe("separator");
  });
}

test("a terminal session: Share once, Public off with its reason, Peer on, no Adopt", () => {
  mount(sess("claude"));
  expect(groups().map((g) => g.group)).toEqual(["Share"]);
  expect(item("share-public")).toMatchObject({
    label: "Public…",
    disabled: true,
    hint: SHARE_PUBLIC_UNAVAILABLE,
  });
  expect(item("share-peer")).toMatchObject({
    label: "Peer…",
    ariaLabel: "Share this terminal's screen with a paired instance",
  });
  expect(item("share-peer")?.disabled).toBeFalsy();
  // The server refuses every terminal session (#1902), so Adopt is not offered.
  expect(item("adopt-mission")).toBeUndefined();
  rulesAreClean();
});

test("an API session: Share once with both entries on, and Adopt to mission", () => {
  mount(sess("claude-api"));
  expect(groups().map((g) => g.group)).toEqual(["Share"]);
  expect(item("share-public")?.disabled).toBeFalsy();
  expect(item("share-public")?.hint).toBeUndefined();
  expect(item("share-peer")?.ariaLabel).toBe("Share this session with a paired instance");
  expect(item("adopt-mission")?.label).toBe("Adopt to mission…");
  // After the primary group, before the mission entry.
  const keys = seen.flatMap((e) => (typeof e !== "string" && "key" in e ? [e.key] : []));
  expect(keys.slice(0, 5)).toEqual(["brief", "handoff", "share-public", "share-peer", "adopt-mission"]);
  rulesAreClean();
});

test("Open mission is offered on a held session whatever its runtime", () => {
  const held = { id: "m1", title: "Ship it", state: "running" } as Session["mission"];
  const { unmount } = mount(sess("claude", { mission: held }));
  expect(item("open-mission")).toBeDefined();
  unmount();
  mount(sess("claude-api", { mission: held }));
  expect(item("open-mission")).toBeDefined();
  expect(item("adopt-mission")).toBeUndefined();
});

test.each([
  ["archived", sess("claude-api", { archived: true })],
  ["a placeholder", { ...sess("claude-api"), id: "claude-api:new-11111111-1111-4111-8111-111111111111" }],
  ["a chat pane", sess("apichat")],
])("no Share group and no Adopt for %s", (_name, s) => {
  mount(s as Session);
  expect(groups()).toEqual([]);
  expect(item("share-public")).toBeUndefined();
  expect(item("share-peer")).toBeUndefined();
  expect(item("adopt-mission")).toBeUndefined();
  rulesAreClean();
});

test("an engine the roster does not know offers neither", () => {
  resetRoster();
  mount(sess("claude-api"));
  expect(groups()).toEqual([]);
  expect(item("adopt-mission")).toBeUndefined();
});

test("omit drops the twins; with both gone the label goes too (#1329)", () => {
  const { unmount } = mount(sess("claude-api"), new Set(["share-public", "share-peer"]));
  expect(groups()).toEqual([]);
  rulesAreClean();
  unmount();
  mount(sess("claude-api"), new Set(["share-public"]));
  expect(groups().map((g) => g.group)).toEqual(["Share"]);
  expect(item("share-public")).toBeUndefined();
  expect(item("share-peer")).toBeDefined();
});

test("Peer and Public open their dialogs from the hook, keyed to the session", async () => {
  const s = sess("claude-api");
  mount(s);
  expect(screen.getByTestId("dialog-open")).toHaveTextContent("false");
  await userEvent.click(screen.getByRole("button", { name: "share-peer" }));
  const peer = screen.getByRole("dialog", { name: "Share with a peer" });
  expect(peer).toHaveAttribute("data-key", s.id);
  expect(peer).toHaveAttribute("data-kind", "api");
  expect(screen.getByTestId("dialog-open")).toHaveTextContent("true");
  await userEvent.click(screen.getByRole("button", { name: "share-public" }));
  expect(screen.getByRole("dialog", { name: "Share publicly" })).toHaveAttribute("data-key", s.id);
  expect(screen.queryByRole("dialog", { name: "Share with a peer" })).toBeNull();
});

test("a terminal session's Peer dialog is the terminal kind", async () => {
  mount(sess("claude"));
  await userEvent.click(screen.getByRole("button", { name: "share-peer" }));
  expect(within(document.body).getByRole("dialog")).toHaveAttribute("data-kind", "terminal");
});

test("the automation opt-out is named for what it does (#2173)", () => {
  const { unmount } = mount(sess("claude"));
  expect(item("orchestrate")?.label).toBe("Exclude from automation");
  unmount();
  mount(sess("claude", { orchestrator_excluded: true }));
  expect(item("orchestrate")?.label).toBe("Include in automation");
});
