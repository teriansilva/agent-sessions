import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import { COLLAPSE_KEY, PublicBar } from "./PublicBar";

afterEach(() => {
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("PublicBar (#2068)", () => {
  test("the mark links to battlelabos.com in a new tab, with no opener and no Referer", () => {
    render(<PublicBar><span>Fix the auth loop</span></PublicBar>);
    const link = screen.getByTestId("public-bar-brand");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "https://battlelabos.com/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")?.split(" ").sort()).toEqual(["noopener", "noreferrer"]);
    expect(link).toHaveAccessibleName(/battlelabos\.com/);
    expect(screen.getByText("Fix the auth loop")).toBeInTheDocument();
  });

  test("collapses to a tab holding the mark and an expand control, kept for the visit", async () => {
    const { unmount } = render(<PublicBar><span>Fix the auth loop</span></PublicBar>);
    const collapse = screen.getByRole("button", { name: "Collapse the header" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(collapse);

    expect(screen.getByTestId("public-bar")).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByText("Fix the auth loop")).not.toBeInTheDocument();
    // The mark still links out from the tab.
    expect(screen.getByTestId("public-bar-brand")).toHaveAttribute("href", "https://battlelabos.com/");
    expect(window.sessionStorage.getItem(COLLAPSE_KEY)).toBe("1");

    // A reload in the same visit opens collapsed.
    unmount();
    render(<PublicBar><span>Fix the auth loop</span></PublicBar>);
    const expand = screen.getByRole("button", { name: "Show the header" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(expand);
    expect(screen.getByText("Fix the auth loop")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(COLLAPSE_KEY)).toBeNull();
  });

  test("storage a browser refuses: the bar still toggles, just not remembered", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    render(<PublicBar />);
    expect(screen.getByTestId("public-bar")).toHaveAttribute("data-collapsed", "false");
    await userEvent.click(screen.getByRole("button", { name: "Collapse the header" }));
    expect(screen.getByTestId("public-bar")).toHaveAttribute("data-collapsed", "true");
  });
});
