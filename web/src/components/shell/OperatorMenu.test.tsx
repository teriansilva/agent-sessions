/** The operator tile (#1058) — three states that must not be confused with each other, and the one
 *  action that ends a session.
 *
 *  "no config yet", "no login at all" and "a login, named or not" are three different facts, and
 *  `auth_mode` — not `username` — is what tells them apart. Rendering any two of them the same way
 *  is the bug this file exists to stop: a tile that says LOCAL on a login-protected install tells
 *  the operator their app is open to the network, and one that offers Sign out where there is no
 *  session offers a control that cannot do anything.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import { OperatorMenu } from "./OperatorMenu";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { logout: vi.fn() } };
});

function mount(props: Partial<React.ComponentProps<typeof OperatorMenu>> = {}) {
  return render(
    <MemoryRouter>
      <OperatorMenu
        username="nightowl"
        authMode="single-user"
        onNavigate={() => {}}
        {...props}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.logout).mockReset().mockResolvedValue(undefined);
});

test("a signed-in operator is named on the tile and in the panel", async () => {
  mount();
  const tile = screen.getByTestId("operator-menu");
  // The ACCESSIBLE name carries the username, so the tile is identifiable at icon-only widths
  // where the visible text is gone.
  expect(tile).toHaveAccessibleName("Operator nightowl");
  expect(tile).toHaveTextContent("NI");

  await userEvent.click(tile);
  expect(screen.getByTestId("operator-who")).toHaveTextContent("nightowl");
  expect(screen.getByTestId("operator-who")).toHaveTextContent(/signed in/i);
});

test("no config yet renders NOTHING, rather than a placeholder identity", () => {
  // A tile that says LOCAL for a frame and then flips to a username is worse than one that
  // arrives late: the wrong statement was made, and it was made about the install's security.
  // `authMode` is the signal — it is the field that is always present once the config lands.
  const { container } = render(
    <MemoryRouter>
      <OperatorMenu
        username={undefined}
        authMode={undefined}
        onNavigate={() => {}}
      />
    </MemoryRouter>,
  );
  expect(container).toBeEmptyDOMElement();
});

test("a login whose NAME is missing is nameless, never LOCAL", async () => {
  // A server too old to send `username` still says `auth_mode: "single-user"`, and that install
  // very much has a login. Reading the missing name as "no login" would tell the operator their
  // app is open to the network and hide the Sign out that still works.
  mount({ username: undefined });
  const tile = screen.getByTestId("operator-menu");
  expect(tile).toHaveAccessibleName("Operator");
  await userEvent.click(tile);
  expect(screen.getByTestId("operator-who")).not.toHaveTextContent(/no login/i);
  expect(screen.getByTestId("operator-sign-out")).toBeInTheDocument();
});

test("a no-login install says LOCAL and offers no Sign out", async () => {
  // `auth_mode: "none"` has no session to end. `SecurityPanel` already hides its own Sign out
  // there; this must agree, or the corner offers a control the app cannot honour.
  mount({ username: null, authMode: "none" });
  const tile = screen.getByTestId("operator-menu");
  expect(tile).toHaveAccessibleName(/no login/i);
  await userEvent.click(tile);
  expect(screen.getByTestId("operator-who")).toHaveTextContent(/no login/i);
  expect(screen.queryByTestId("operator-sign-out")).toBeNull();
  // Settings is still reachable from here — that is what keeps the gear one tap from the corner
  // on a phone, where the action cluster itself has moved into the drawer.
  expect(screen.getByRole("menuitem", { name: "Settings" })).toHaveAttribute(
    "href",
    "/settings",
  );
});

test("a server that sends auth_mode none but still names a user is treated as no-login", async () => {
  // Defence in depth against a mismatch between the two fields: the MODE decides. A tile that
  // offered Sign out because a name happened to be present would be acting on the wrong one.
  mount({ username: "admin", authMode: "none" });
  await userEvent.click(screen.getByTestId("operator-menu"));
  expect(screen.queryByTestId("operator-sign-out")).toBeNull();
});

test("Sign out calls logout, and the menu items are real links", async () => {
  mount();
  await userEvent.click(screen.getByTestId("operator-menu"));
  expect(
    screen.getByRole("menuitem", { name: /security/i }),
  ).toHaveAttribute("href", "/settings/security");
  await userEvent.click(screen.getByTestId("operator-sign-out"));
  expect(api.logout).toHaveBeenCalledTimes(1);
});

test("a failed sign-out does not take the app down", async () => {
  // `api.logout` hard-navigates on success. On failure the operator stays where they are — an
  // unhandled rejection here would surface as a console error in every session that lost its
  // network at the wrong moment.
  vi.mocked(api.logout).mockRejectedValue(new Error("offline"));
  mount();
  await userEvent.click(screen.getByTestId("operator-menu"));
  await userEvent.click(screen.getByTestId("operator-sign-out"));
  expect(api.logout).toHaveBeenCalled();
});
