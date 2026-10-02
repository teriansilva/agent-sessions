import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { DOCS_HOME_URL } from "../lib/links";
import { newestRelease } from "./due";
import { WhatsNewDialog } from "./WhatsNewDialog";

const release = newestRelease()!;

function renderDialog() {
  const onDismiss = vi.fn();
  const onNavigate = vi.fn();
  render(<WhatsNewDialog release={release} onDismiss={onDismiss} onNavigate={onNavigate} />);
  return { onDismiss, onNavigate, dialog: screen.getByRole("dialog", { name: /what's new/i }) };
}

const next = () => userEvent.click(screen.getByRole("button", { name: /^(next|show me)/i }));

test("opens on the intro, and the intro's tiles jump to their slide", async () => {
  renderDialog();
  expect(screen.getByRole("heading", { name: "The deck runs missions now." })).toBeInTheDocument();
  expect(screen.getByText("1 / 7")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^templates/i }));
  expect(screen.getByRole("heading", { name: /write it once/i })).toBeInTheDocument();
  expect(screen.getByText("5 / 7")).toBeInTheDocument();
});

test("Next, Back and the arrow keys move between slides", async () => {
  renderDialog();
  await next();
  expect(screen.getByText("2 / 7")).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "ArrowRight" });
  expect(screen.getByText("3 / 7")).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "ArrowLeft" });
  await userEvent.click(screen.getByRole("button", { name: /back/i }));
  expect(screen.getByText("1 / 7")).toBeInTheDocument();
});

test("the last slide's button dismisses", async () => {
  const { onDismiss } = renderDialog();
  for (let i = 0; i < 6; i++) await next();
  expect(screen.getByText("7 / 7")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Let's go" }));
  expect(onDismiss).toHaveBeenCalledTimes(1);
});

test("✕ and Escape dismiss; a click on the backdrop does not", async () => {
  const { onDismiss, dialog } = renderDialog();
  await userEvent.click(dialog.parentElement!);
  expect(onDismiss).not.toHaveBeenCalled();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(onDismiss).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Close what's new" }));
  expect(onDismiss).toHaveBeenCalledTimes(2);
});

test("an in-app CTA hands its route to the shell", async () => {
  const { onNavigate } = renderDialog();
  await next();
  await userEvent.click(screen.getByRole("button", { name: /open missions/i }));
  expect(onNavigate).toHaveBeenCalledWith("/mission");
});

test("the docs CTA opens the docs home in a new tab, without an opener, and dismisses", async () => {
  const { onDismiss } = renderDialog();
  for (let i = 0; i < 5; i++) await next();
  const link = screen.getByRole("link", { name: /open the docs/i });
  expect(link).toHaveAttribute("href", DOCS_HOME_URL);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  link.addEventListener("click", (e) => e.preventDefault());
  await userEvent.click(link);
  expect(onDismiss).toHaveBeenCalledTimes(1);
});

test("Pause swaps the illustration to its still variant and says it is pressed", async () => {
  const { dialog } = renderDialog();
  await next();
  const img = () => dialog.querySelector("img")!;
  expect(img().getAttribute("src")).toMatch(/whatsnew\/0\.20\/missions\.svg$/);
  const pause = screen.getByRole("button", { name: "Pause animation" });
  expect(pause).toHaveAttribute("aria-pressed", "false");
  await userEvent.click(pause);
  expect(pause).toHaveAttribute("aria-pressed", "true");
  expect(img().getAttribute("src")).toMatch(/missions-still\.svg$/);
  await next();
  expect(img().getAttribute("src")).toMatch(/files-still\.svg$/);
});

test("the page behind is inert while it is open, and released on unmount", () => {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  const { unmount } = render(
    <WhatsNewDialog release={release} onDismiss={() => {}} onNavigate={() => {}} />,
  );
  expect(root.hasAttribute("inert")).toBe(true);
  unmount();
  expect(root.hasAttribute("inert")).toBe(false);
  root.remove();
});
