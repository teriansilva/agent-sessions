import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

// Settings › Profile (#1931): upload through the browser prep, remove, and the preview follows the
// SERVER's version (the config), never the chosen file.
const cfg = vi.hoisted(() => ({
  current: { avatar: null as string | null, username: "marcus", operator_name: null as string | null },
  reload: vi.fn(async () => true),
}));
vi.mock("../app/config", () => ({
  useConfig: () => cfg.current,
  useConfigReload: () => cfg.reload,
}));
const api = vi.hoisted(() => ({
  uploadAvatar: vi.fn(),
  deleteAvatar: vi.fn(),
  avatarBlob: vi.fn(),
  setOperatorName: vi.fn(),
}));
vi.mock("../lib/api", async (orig) => ({ ...(await orig<typeof import("../lib/api")>()), api }));
const prep = vi.hoisted(() => ({ squareAvatar: vi.fn() }));
vi.mock("../lib/avatarImage", async (orig) => ({
  ...(await orig<typeof import("../lib/avatarImage")>()),
  squareAvatar: prep.squareAvatar,
}));

const { ProfileSettings } = await import("./ProfileSettings");
const { resetOperatorAvatarCache } = await import("../lib/operatorAvatar");
const { AvatarImageError } = await import("../lib/avatarImage");
const { ApiError } = await import("../lib/api");

beforeEach(() => {
  cfg.current = { avatar: null, username: "marcus", operator_name: null };
  api.setOperatorName.mockReset();
  cfg.reload.mockClear();
  api.uploadAvatar.mockReset();
  api.deleteAvatar.mockReset();
  api.avatarBlob.mockReset();
  api.avatarBlob.mockImplementation(async (v: string) => new Blob([v], { type: "image/png" }));
  resetOperatorAvatarCache();
  URL.createObjectURL = vi.fn((b: Blob) => `blob:avatar-${(b as Blob).size}`);
  URL.revokeObjectURL = vi.fn();
  prep.squareAvatar.mockReset();
});

const file = () => new File([new Uint8Array([1, 2, 3])], "me.png", { type: "image/png" });

describe("ProfileSettings", () => {
  test("no avatar: the initial, Upload, no Remove", () => {
    const { container } = render(<ProfileSettings />);
    expect(container.querySelector('[data-avatar][data-kind="accent"]')?.textContent).toBe("M");
    expect(screen.getByRole("button", { name: "Upload image" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
  });

  test("a chosen file is prepared, uploaded, and the config re-read", async () => {
    const prepared = new Blob(["x"], { type: "image/webp" });
    prep.squareAvatar.mockResolvedValue(prepared);
    api.uploadAvatar.mockResolvedValue({ version: "v1" });
    render(<ProfileSettings />);
    await userEvent.upload(screen.getByTestId("profile-avatar-input"), file());
    await waitFor(() => expect(api.uploadAvatar).toHaveBeenCalledWith(prepared));
    expect(cfg.reload).toHaveBeenCalled();
  });

  test("an image is shown from the server's version, and Remove deletes it", async () => {
    cfg.current = { avatar: "abc", username: "marcus", operator_name: null };
    api.deleteAvatar.mockResolvedValue({ version: null });
    const { container } = render(<ProfileSettings />);
    // Through the fetch seam (the Home Free tunnel in app mode), shown as an object URL.
    await waitFor(() =>
      expect(container.querySelector("[data-avatar] img")?.getAttribute("src")).toBe("blob:avatar-3"),
    );
    expect(api.avatarBlob).toHaveBeenCalledWith("abc");
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(api.deleteAvatar).toHaveBeenCalled());
    expect(cfg.reload).toHaveBeenCalled();
  });

  test("a refusal, from the browser or the server, is said", async () => {
    prep.squareAvatar.mockRejectedValueOnce(new AvatarImageError("Choose a PNG, JPEG or WebP image."));
    render(<ProfileSettings />);
    await userEvent.upload(screen.getByTestId("profile-avatar-input"), file());
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a PNG, JPEG or WebP image.");
    prep.squareAvatar.mockResolvedValue(new Blob(["x"], { type: "image/webp" }));
    api.uploadAvatar.mockRejectedValue(new ApiError(415, "only PNG, JPEG or WebP images"));
    await userEvent.upload(screen.getByTestId("profile-avatar-input"), file());
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("only PNG, JPEG or WebP images"),
    );
  });

  test("a display name is saved trimmed, and the disc takes its initial (#1942)", async () => {
    api.setOperatorName.mockResolvedValue({ operator_name: "Jo Doe" });
    render(<ProfileSettings />);
    const field = screen.getByTestId("profile-display-name");
    expect(field).toHaveValue("");
    expect(field).toHaveAttribute("maxlength", "40");
    await userEvent.type(field, "  Jo Doe ");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.setOperatorName).toHaveBeenCalledWith("Jo Doe"));
    expect(cfg.reload).toHaveBeenCalled();
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  test("the server's reason for refusing a name is said", async () => {
    api.setOperatorName.mockRejectedValue(
      new ApiError(422, "operator_name must be 1–40 printable characters"),
    );
    render(<ProfileSettings />);
    await userEvent.type(screen.getByTestId("profile-display-name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByTestId("profile-name-error")).toHaveTextContent(
      "operator_name must be 1–40 printable characters",
    );
  });

  test("the disc's initial follows a stored display name", () => {
    cfg.current = { avatar: null, username: "marcus", operator_name: "Zed" };
    const { container } = render(<ProfileSettings />);
    expect(container.querySelector('[data-avatar][data-kind="accent"]')?.textContent).toBe("Z");
    expect(screen.getByTestId("profile-display-name")).toHaveValue("Zed");
  });
});
