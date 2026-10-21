import { useEffect, useState } from "react";
import { ApiError, api } from "../../lib/api";
import { playbookPath, SKILLS_PATH } from "../../lib/routes";
import type { GalleryKind, GalleryOperation } from "../../types/gallery";

export const PENDING = "battlelab-gallery-local-operation-v1";
/** What the pending operation was adding, for the banner's wording only (#1440). It is read only
 *  while it names the same operation ID, so it can never describe a different attempt. */
export const PENDING_LABEL = "battlelab-gallery-local-operation-label-v1";
export const localPath = (kind: GalleryKind, id: string) =>
  kind === "playbook"
    ? playbookPath(id)
    : `${SKILLS_PATH}/${encodeURIComponent(id)}`;
export const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
export function pendingId() {
  try {
    const value = localStorage.getItem(PENDING);
    return value && /^[0-9a-f]{32}$/.test(value) ? value : null;
  } catch {
    return null;
  }
}
export function pendingLabel(id: string): string | null {
  try {
    const value = JSON.parse(localStorage.getItem(PENDING_LABEL) ?? "null") as unknown;
    if (value && typeof value === "object" && "operation" in value && "label" in value) {
      const { operation, label } = value as { operation: unknown; label: unknown };
      if (operation === id && typeof label === "string") return label.slice(0, 200);
    }
  } catch {
    /* an unreadable label only loses the wording */
  }
  return null;
}
export function beginOperation(label?: string) {
  if (pendingId())
    throw new Error(
      "Check the pending operation before starting another import or update.",
    );
  const id = crypto.randomUUID().replaceAll("-", "");
  localStorage.setItem(PENDING, id);
  if (label) localStorage.setItem(PENDING_LABEL, JSON.stringify({ operation: id, label }));
  return id;
}
export function finishOperation(result: GalleryOperation) {
  if (result.state === "complete") forgetOperation(result.operation);
}
export function forgetOperation(id: string) {
  if (pendingId() === id) {
    localStorage.removeItem(PENDING);
    localStorage.removeItem(PENDING_LABEL);
  }
}
/** After the server ANSWERED a write with a refusal: forget the operation only when the server
 *  confirms it never recorded it. The request has finished, so no record can still appear; a
 *  recorded or unreadable operation stays pending for the banner's explicit check. */
export async function settleRefused(id: string): Promise<boolean> {
  try {
    await api.galleryOperation(id);
    return false;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      forgetOperation(id);
      return true;
    }
    return false;
  }
}
/** The pending operation ID, kept current across tabs and banners. */
export function usePendingId() {
  const [id, setId] = useState(pendingId);
  useEffect(() => {
    const timer = window.setInterval(() => setId(pendingId()), 700);
    return () => clearInterval(timer);
  }, []);
  return [id, setId] as const;
}

export function ownValue<T>(files: Record<string, T>, name: string): T | undefined {
  return Object.hasOwn(files, name) ? files[name] : undefined;
}
