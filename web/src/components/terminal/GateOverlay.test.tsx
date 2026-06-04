import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { GateOverlay } from "./GateOverlay";

test("busy mode: 'Session in use' + who holds it, Take over / Cancel wired (#293)", async () => {
  const user = userEvent.setup();
  const onTakeover = vi.fn();
  const onCancel = vi.fn();
  render(
    <GateOverlay
      holder={{ label: "Mac · Chrome" }}
      mode="busy"
      onTakeover={onTakeover}
      onCancel={onCancel}
    />,
  );
  expect(screen.getByText(/session in use/i)).toBeInTheDocument();
  expect(screen.getByText("Mac · Chrome")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /take over/i }));
  expect(onTakeover).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onCancel).toHaveBeenCalledOnce();
});

test("taken mode: 'Control taken' + who took it (#293)", () => {
  render(
    <GateOverlay
      holder={{ label: "iPhone · Safari" }}
      mode="taken"
      onTakeover={() => {}}
      onCancel={() => {}}
    />,
  );
  expect(screen.getByText(/control taken/i)).toBeInTheDocument();
  expect(screen.getByText("iPhone · Safari")).toBeInTheDocument();
});

test("Escape triggers Cancel (→ new session) (#293)", () => {
  const onCancel = vi.fn();
  render(<GateOverlay holder={null} mode="busy" onTakeover={() => {}} onCancel={onCancel} />);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onCancel).toHaveBeenCalledOnce();
});

test("a missing/blank holder label degrades to 'another device' (#293)", () => {
  render(<GateOverlay holder={{ label: "" }} mode="busy" onTakeover={() => {}} onCancel={() => {}} />);
  expect(screen.getByText("another device")).toBeInTheDocument();
});

test("the holder label is rendered as text, not HTML (untrusted, #293)", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const { container } = render(
    <GateOverlay holder={{ label: evil }} mode="busy" onTakeover={() => {}} onCancel={() => {}} />,
  );
  expect(screen.getByText(evil)).toBeInTheDocument(); // shown verbatim as text
  expect(container.querySelector("img")).toBeNull(); // never parsed into a node
});
