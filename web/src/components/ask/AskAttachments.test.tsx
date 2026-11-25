/** The file cards (#1707) — the fallback contract and the download seam.
 *
 *  Pinned here: a gone file (verified 404 from the tiny `?meta=1` probe) shows its name and
 *  *gone* with NO Download for EVERY kind (the old code never probed `other`, so a deleted PDF
 *  kept an apparently working link); a decode failure with bytes present keeps Download; and
 *  Download fetches through the app's api seam — never a native `/api/…` href, which in Home
 *  Free app mode would ask the relay for the blob (the `lib/uploadObjectUrl.ts` rule).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { AttachmentCard } from "./AskAttachments";
import { api, ApiError } from "../../lib/api";

const CID = "ask_" + "a".repeat(32);

const att = (kind: "image" | "text" | "other") => ({
  id: "b".repeat(32),
  name: kind === "other" ? "release.pdf" : kind === "image" ? "shot.png" : "deploy.log",
  kind,
  size: 12,
});

beforeEach(() => {
  vi.restoreAllMocks();
});

test("a gone file of ANY kind drops its Download and says gone", async () => {
  vi.spyOn(api, "askAttachmentMeta").mockRejectedValue(new ApiError(404, "gone"));
  render(<AttachmentCard cid={CID} att={att("other")} />);
  await waitFor(() => expect(screen.getByText("gone")).toBeTruthy());
  expect(screen.queryByTestId("ask-attachment-download")).toBeNull();
});

test("a present download-only kind shows Download and never previews bytes", async () => {
  const meta = vi
    .spyOn(api, "askAttachmentMeta")
    .mockResolvedValue({ attachment: att("other") });
  const blob = vi.spyOn(api, "askAttachmentBlob");
  render(<AttachmentCard cid={CID} att={att("other")} />);
  await waitFor(() => expect(screen.getByTestId("ask-attachment-download")).toBeTruthy());
  expect(meta).toHaveBeenCalledTimes(1); // the tiny probe, not a download
  expect(blob).not.toHaveBeenCalled(); // no eager bytes for a download-only kind
});

test("Download fetches through the api seam — never a native /api href", async () => {
  vi.spyOn(api, "askAttachmentMeta").mockResolvedValue({ attachment: att("other") });
  const blob = vi
    .spyOn(api, "askAttachmentBlob")
    .mockResolvedValue(new Blob(["pdf bytes"]));
  render(<AttachmentCard cid={CID} att={att("other")} />);
  await waitFor(() => expect(screen.getByTestId("ask-attachment-download")).toBeTruthy());
  const anchor = screen.getByTestId("ask-attachment-download");
  expect(anchor.tagName).toBe("BUTTON"); // no href to leak through a relay
  fireEvent.click(anchor);
  await waitFor(() => expect(blob).toHaveBeenCalledTimes(1));
});

test("a decode failure with bytes present keeps Download", async () => {
  // jsdom never decodes an <img>, so the failure is fired explicitly — the contract being
  // pinned is the REACTION to a failed decode: name + Download stay, no spinner, no gone.
  vi.spyOn(api, "askAttachmentMeta").mockResolvedValue({ attachment: att("image") });
  vi.spyOn(api, "askAttachmentBlob").mockResolvedValue(new Blob([new Uint8Array([1, 2, 3])]));
  // jsdom has no object-URL machinery; the browser's is stubbed here.
  const create = vi.fn(() => "blob:test");
  const revoke = vi.fn();
  vi.stubGlobal("URL", { ...URL, createObjectURL: create, revokeObjectURL: revoke });
  render(<AttachmentCard cid={CID} att={att("image")} />);
  await waitFor(() => expect(screen.getByTestId("ask-attachment-img")).toBeTruthy());
  fireEvent.error(screen.getByTestId("ask-attachment-img"));
  await waitFor(() => expect(screen.getByTestId("ask-attachment-fallback")).toBeTruthy());
  expect(screen.getByTestId("ask-attachment-download")).toBeTruthy();
  expect(screen.queryByText("gone")).toBeNull();
});

test("a file shared as a secret offers no View, preview or Download and never fetches bytes", async () => {
  const secret = { ...att("text"), name: "notes.txt", secret: true };
  vi.spyOn(api, "askAttachmentMeta").mockResolvedValue({ attachment: secret });
  const blob = vi.spyOn(api, "askAttachmentBlob");
  render(<AttachmentCard cid={CID} att={secret} expanded />);
  await waitFor(() => expect(screen.getByTestId("ask-attachment-secret")).toBeTruthy());
  expect(screen.getByText(/Secret ·/)).toBeTruthy();
  expect(screen.queryByTestId("ask-attachment-view")).toBeNull();
  expect(screen.queryByTestId("ask-attachment-download")).toBeNull();
  expect(screen.queryByTestId("ask-attachment-preview")).toBeNull();
  expect(blob).not.toHaveBeenCalled();
});
