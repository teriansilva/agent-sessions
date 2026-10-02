import { render, screen } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { SessionWindow } from "./SessionWindow";

// The window mounts the SAME <Terminal> the /s/:engine/:id route uses; capture the handoff
// callbacks it is given without mounting xterm.
type Captured = {
  onOpenGallery?: (to: string) => void;
  onSaveAsTemplate?: (draft: { body: string; images: { name: string; path: string }[] }) => void;
};
const captured: Captured[] = [];
vi.mock("../terminal/Terminal", () => ({
  Terminal: (props: Captured) => {
    captured.push(props);
    return <div data-testid="term" />;
  },
}));

function LocationProbe() {
  const loc = useLocation();
  return (
    <div data-testid="loc">
      {loc.pathname}|{JSON.stringify(loc.state)}
    </div>
  );
}

beforeEach(() => {
  captured.length = 0;
});

test("an Overview window's terminal gets the router-backed gallery and save-as-template handoffs — never a document navigation (#908 round 9)", async () => {
  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <Routes>
        <Route
          path="/overview"
          element={
            <SessionWindow
              wkey="claude:abc"
              engine="claude"
              id="abc"
              title="A session"
              rect={{ x: 0, y: 0, w: 600, h: 400 }}
              bounds={{ w: 1200, h: 800 }}
              focused
              role="owner"
              onFocus={() => {}}
              onClose={() => {}}
              onFullScreen={() => {}}
              onRect={() => {}}
              onRole={() => {}}
            />
          }
        />
        <Route path="/templates" element={<p>gallery route</p>} />
        <Route path="/templates/new" element={<p>editor route</p>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("term")).toBeInTheDocument();
  const props = captured[captured.length - 1];
  // Unfixed: neither callback was passed, so the picker's <a href> hard-navigated.
  expect(typeof props.onOpenGallery).toBe("function");
  expect(typeof props.onSaveAsTemplate).toBe("function");
  await act(async () => {
    props.onOpenGallery!("/templates");
  });
  expect(screen.getByText("gallery route")).toBeInTheDocument(); // an in-app route change
  await act(async () => {
    props.onSaveAsTemplate!({ body: "keep me", images: [] });
  });
  expect(screen.getByText("editor route")).toBeInTheDocument();
  expect(screen.getByTestId("loc")).toHaveTextContent('/templates/new|{"prefill":{"body":"keep me","images":[]}}');
});
