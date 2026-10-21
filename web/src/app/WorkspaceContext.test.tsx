import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { WORKSPACE_KEY } from "../components/overview/windowStore";
import { CAP_KEY } from "../components/overview/windowStore";
import { WorkspaceProvider } from "./WorkspaceContext";
import { useMapWindows } from "./workspaceWindows";

/** `hasRoom`, the new-session form's capacity precheck (#936, Hermes round 2).
 *
 *  Pinned HERE rather than in the browser suite, and that is deliberate. In the browser the
 *  guarantee it protects — a launch is never lost — is also held by the drain's rejection
 *  fallback, so reverting this arithmetic still ends with the session running full screen and the
 *  browser test still passes. What the precheck alone decides is whether the operator is bounced
 *  through the map on the way there, which is a transient nobody can assert reliably. Its
 *  arithmetic is deterministic, so it is asserted directly.
 */

function Probe() {
  const ws = useMapWindows();
  return <div data-testid="room">{String(ws?.hasRoom)}</div>;
}

const layout = (n: number) =>
  JSON.stringify(
    Array.from({ length: n }, (_, i) => ({
      key: `claude:s${i}`,
      engine: "claude",
      id: `s${i}`,
      title: `S${i}`,
      x: 0,
      y: 0,
      w: 700,
      h: 500,
      z: i,
    })),
  );

const room = () => screen.getByTestId("room").textContent;

describe("hasRoom", () => {
  beforeEach(() => localStorage.clear());

  it("counts the layout still awaiting restoration, not just the open windows", () => {
    // The reachable case is a RELOADED new-session form: the provider is fresh, so `windows` is
    // empty and the whole saved layout is still in `restorable`. Counting only `windows` reads as
    // "plenty of room", hands the launch to a map that is about to fill its own cap, and the open
    // is then refused on arrival.
    localStorage.setItem(CAP_KEY, "1");
    localStorage.setItem(WORKSPACE_KEY, layout(1));
    render(
      <StrictMode>
        <WorkspaceProvider>
          <Probe />
        </WorkspaceProvider>
      </StrictMode>,
    );
    expect(room()).toBe("false");
  });

  it("says there is room when the stored layout leaves some", () => {
    localStorage.setItem(CAP_KEY, "3");
    localStorage.setItem(WORKSPACE_KEY, layout(1));
    render(
      <StrictMode>
        <WorkspaceProvider>
          <Probe />
        </WorkspaceProvider>
      </StrictMode>,
    );
    expect(room()).toBe("true");
  });

  it("with nothing stored, room is decided by the cap alone", () => {
    localStorage.setItem(CAP_KEY, "1");
    render(
      <StrictMode>
        <WorkspaceProvider>
          <Probe />
        </WorkspaceProvider>
      </StrictMode>,
    );
    expect(room()).toBe("true");
  });
});

/** `room` + `openKeys`, Ask's batch admission (Hermes on #1320): before the map has restored, the
 *  stored layout counts as OPEN as well as against the room — asking for one of those sessions is
 *  a focus by the restore, never a refused new slot. */
function KeysProbe() {
  const ws = useMapWindows();
  return (
    <div data-testid="keys" data-room={ws?.room}>
      {[...(ws?.openKeys ?? [])].sort().join(",")}
    </div>
  );
}

describe("room and openKeys", () => {
  beforeEach(() => localStorage.clear());

  it("a stored layout filling the cap leaves no room, and every stored session reads as open", () => {
    localStorage.setItem(CAP_KEY, "2");
    localStorage.setItem(WORKSPACE_KEY, layout(2));
    render(
      <StrictMode>
        <WorkspaceProvider>
          <KeysProbe />
        </WorkspaceProvider>
      </StrictMode>,
    );
    const el = screen.getByTestId("keys");
    expect(el).toHaveAttribute("data-room", "0");
    expect(el).toHaveTextContent("claude:s0,claude:s1");
  });

  it("with nothing stored, nothing is open and the room is the cap", () => {
    localStorage.setItem(CAP_KEY, "3");
    render(
      <WorkspaceProvider>
        <KeysProbe />
      </WorkspaceProvider>,
    );
    const el = screen.getByTestId("keys");
    expect(el).toHaveAttribute("data-room", "3");
    expect(el).toHaveTextContent(/^$/);
  });
});
