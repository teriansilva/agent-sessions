import { playbookPath, SKILLS_PATH } from "../../lib/routes";
import type { GalleryKind, GalleryOperation } from "../../types/gallery";

export const PENDING = "battlelab-gallery-local-operation-v1";
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
export function beginOperation() {
  if (pendingId())
    throw new Error(
      "Check the pending operation before starting another import or update.",
    );
  const id = crypto.randomUUID().replaceAll("-", "");
  localStorage.setItem(PENDING, id);
  return id;
}
export function finishOperation(result: GalleryOperation) {
  if (result.state === "complete") forgetOperation(result.operation);
}
export function forgetOperation(id: string) {
  if (pendingId() === id) localStorage.removeItem(PENDING);
}

export function ownValue<T>(files: Record<string, T>, name: string): T | undefined {
  return Object.hasOwn(files, name) ? files[name] : undefined;
}
