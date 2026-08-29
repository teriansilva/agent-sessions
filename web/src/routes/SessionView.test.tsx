import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { SessionView } from "./SessionView";

// Capture the props the real Terminal would receive (engine/id/onReconcileId) without
// mounting xterm. The mock also renders the id so we can assert it stays frozen.
const reconcileHandlers: ((sid: string) => void)[] = [];
vi.mock("../components/terminal/Terminal", () => ({
  Terminal: ({
    engine,
    id,
    rowKey,
    onReconcileId,
  }: {
    engine: string;
    id: string;
    rowKey?: string;
    onReconcileId?: (sid: string) => void;
  }) => {
    if (onReconcileId) reconcileHandlers.push(onReconcileId);
    return (
      <>
        <div data-testid="term">{`${engine}:${id}`}</div>
        {/* What the header will look the row up under (#867). The identity above stays frozen
            on the placeholder so the socket survives; this must follow the URL to the real id,
            or the header/Recap/Hand off stay nameless for the rest of a converged session. */}
        <div data-testid="term-rowkey">{rowKey ?? "—"}</div>
      </>
    );
  },
}));

// Surfaces the current location so the test can assert the URL converged.
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname}</div>;
}

// Lets a test drive genuine in-SPA navigations (no reload) to arbitrary sessions.
function Nav() {
  const navigate = useNavigate();
  return (
    <>
      <button onClick={() => navigate("/s/claude/cla_2222")}>go-claude</button>
      <button onClick={() => navigate("/s/opencode/ses_realreal0000")}>
        go-back
      </button>
    </>
  );
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/s/:engine/:id" element={<SessionView />} />
      </Routes>
      <Nav />
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  reconcileHandlers.length = 0;
});

test("opens under the placeholder id from the URL", () => {
  renderAt("/s/opencode/new-1111");
  expect(screen.getByTestId("term").textContent).toBe("opencode:new-1111");
});

test("reconcile converges the URL to the real id without changing the terminal identity", () => {
  renderAt("/s/opencode/new-11111111-1111-1111-1111-111111111111");
  expect(screen.getByTestId("term").textContent).toBe(
    "opencode:new-11111111-1111-1111-1111-111111111111",
  );

  // Server reconciled → the terminal calls onReconcileId with the real id.
  act(() => {
    reconcileHandlers[0]("opencode:ses_realreal0000");
  });

  // URL converged to the real id (history replace, no reload)…
  expect(screen.getByTestId("loc").textContent).toBe(
    "/s/opencode/ses_realreal0000",
  );
  // …but the terminal keeps its ORIGINAL identity (frozen key/props) so the live socket
  // is preserved — no remount, no relaunch.
  expect(screen.getByTestId("term").textContent).toBe(
    "opencode:new-11111111-1111-1111-1111-111111111111",
  );
});

test("navigating back to a reconciled real id (after another session) re-opens it", async () => {
  // Hermes #131: the converge-suppression must be one-shot. Reproduce the leak it caught —
  // placeholder → reconcile real id → visit a Claude session → navigate back to the real id.
  renderAt("/s/opencode/new-11111111-1111-1111-1111-111111111111");
  act(() => {
    reconcileHandlers[0]("opencode:ses_realreal0000");
  });
  expect(screen.getByTestId("loc").textContent).toBe(
    "/s/opencode/ses_realreal0000",
  );

  // Genuine navigation away → the terminal adopts the Claude session (new key → remount).
  await userEvent.click(screen.getByText("go-claude"));
  expect(screen.getByTestId("term").textContent).toBe("claude:cla_2222");

  // Navigate back to the reconciled real opencode id. Before the fix this stayed stuck on
  // the Claude session (the real id lingered in `converged`); now it re-opens as a plain
  // attach by the real id.
  await userEvent.click(screen.getByText("go-back"));
  expect(screen.getByTestId("term").textContent).toBe(
    "opencode:ses_realreal0000",
  );
  expect(screen.getByTestId("loc").textContent).toBe(
    "/s/opencode/ses_realreal0000",
  );
});

// #867 review (blocker 2): the frozen identity is what preserves the socket, but the session ROW
// only ever exists under the REAL id. Resolving header metadata from the frozen key alone left
// the header, Recap and Hand off nameless for the rest of a converged session's life — the file
// panel recovered only because SessionView already looked under both keys.
test("after the converge the terminal is told the REAL key to look its row up under (#867)", () => {
  renderAt("/s/opencode/new-11111111-1111-1111-1111-111111111111");
  expect(screen.getByTestId("term-rowkey").textContent).toBe(
    "opencode:new-11111111-1111-1111-1111-111111111111",
  );

  act(() => {
    reconcileHandlers[0]("opencode:ses_realreal0000");
  });

  // Identity frozen (socket preserved) …
  expect(screen.getByTestId("term").textContent).toBe(
    "opencode:new-11111111-1111-1111-1111-111111111111",
  );
  // … row key follows the URL, which is the only place the row exists.
  expect(screen.getByTestId("term-rowkey").textContent).toBe(
    "opencode:ses_realreal0000",
  );
});
