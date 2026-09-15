import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { DOCS_HOME_URL } from "../../lib/links";

import { HelpMenu } from "./HelpMenu";

function setup(whatsNewLabel: string | null = "What's new in 0.20") {
  const onTour = vi.fn();
  const onWhatsNew = vi.fn();
  render(
    <HelpMenu
      onTour={onTour}
      onWhatsNew={onWhatsNew}
      whatsNewLabel={whatsNewLabel}
    />,
  );
  return { onTour, onWhatsNew, user: userEvent.setup() };
}

test("the ? opens a Help menu with the tour, the docs and What's new, in that order", async () => {
  const { user } = setup();
  const trigger = screen.getByRole("button", { name: "Help" });
  expect(trigger).toHaveAttribute("aria-haspopup", "menu");
  expect(trigger).toHaveAttribute("aria-expanded", "false");

  await user.click(trigger);

  expect(trigger).toHaveAttribute("aria-expanded", "true");
  const items = screen.getAllByRole("menuitem");
  expect(items.map((i) => i.getAttribute("aria-label") ?? i.textContent)).toEqual([
    "Intro tour",
    "Documentation (opens in a new tab)",
    "What's new in 0.20",
  ]);
  expect(items[0]).toHaveFocus();
});

test("Documentation is the docs home, in a new tab with no opener", async () => {
  const { user } = setup();
  await user.click(screen.getByRole("button", { name: "Help" }));

  const docs = screen.getByRole("menuitem", {
    name: "Documentation (opens in a new tab)",
  });
  expect(docs).toHaveAttribute("href", DOCS_HOME_URL);
  expect(docs).toHaveAttribute("target", "_blank");
  expect(docs).toHaveAttribute("rel", "noopener noreferrer");
});

test("with no bundled release notes there is no What's new item", async () => {
  const { user } = setup(null);
  await user.click(screen.getByRole("button", { name: "Help" }));

  expect(screen.getAllByRole("menuitem")).toHaveLength(2);
  expect(screen.queryByRole("menuitem", { name: /what's new/i })).toBeNull();
});

test("choosing an item runs it and closes the menu", async () => {
  const { user, onTour, onWhatsNew } = setup();

  await user.click(screen.getByRole("button", { name: "Help" }));
  await user.click(screen.getByRole("menuitem", { name: "Intro tour" }));
  expect(onTour).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).toBeNull();

  await user.click(screen.getByRole("button", { name: "Help" }));
  await user.click(screen.getByRole("menuitem", { name: "What's new in 0.20" }));
  expect(onWhatsNew).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).toBeNull();
});
