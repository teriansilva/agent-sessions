import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import { NewVersionBanner } from "./NewVersionBanner";

vi.mock("../lib/api", () => ({ api: { version: vi.fn() } }));
const mockVersion = vi.mocked(api.version);

beforeEach(() => {
  mockVersion.mockReset();
});

test("renders nothing while the version is unchanged (#169)", async () => {
  mockVersion.mockResolvedValue({ version: "v1" });
  const { container } = render(<NewVersionBanner />);
  // No banner ever appears while the polled version matches the initial one.
  await waitFor(() => expect(mockVersion).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});

test("renders a banner with a Reload button after a version change (#169)", async () => {
  // Initial poll → v1, second poll → v2 simulating a deploy.
  mockVersion.mockResolvedValueOnce({ version: "v1" });
  mockVersion.mockResolvedValueOnce({ version: "v2" });
  // Visibility-change triggers an extra check, which surfaces the change immediately.
  let hidden = true;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  render(<NewVersionBanner />);
  await waitFor(() => expect(mockVersion).toHaveBeenCalledTimes(1)); // initial pin
  hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent(/new version is available/i),
  );
  expect(screen.getByRole("button", { name: /reload/i })).toBeInTheDocument();
});

test("clicking Reload calls location.reload (#169)", async () => {
  const reload = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
  mockVersion.mockResolvedValueOnce({ version: "v1" });
  mockVersion.mockResolvedValueOnce({ version: "v2" });
  let hidden = true;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  render(<NewVersionBanner />);
  await waitFor(() => expect(mockVersion).toHaveBeenCalledTimes(1));
  hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  const btn = await screen.findByRole("button", { name: /reload/i });
  await userEvent.click(btn);
  expect(reload).toHaveBeenCalledTimes(1);
});
