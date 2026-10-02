import { ApiError } from "../../lib/api";
import type { PluginGeneration, PluginOperation, PluginReview } from "../../types/plugins";

export const SETUP_PATH = "/settings/agents/setup/new";
export const AGENTS_PATH = "/settings/agents";
export const setupPath = (params: Record<string, string> = {}) => `${SETUP_PATH}?${new URLSearchParams(params)}`;
export const pendingOperation = (op: PluginOperation | null | undefined) =>
  !!op && ["planned", "running", "ready", "cleanup_pending"].includes(op.state);
export const verifiedGeneration = (gen: PluginGeneration | null | undefined) =>
  !!gen?.verification && gen.verification.digest === gen.review.digest &&
  gen.required_checks.every(check => gen.verification?.results.some(r => r.check === check && r.passed));
export function sourceLabel(review: PluginReview): string {
  return `${review.adopted_path ? "Adopted · " : ""}${review.source === "signed" ? "Signed source" : "Local · Untrusted"}`;
}
export const pluginError = (error: unknown) => error instanceof Error ? error.message : "The operation could not complete.";

export const rejectedOperation = (error: unknown) => error instanceof ApiError &&
  error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status);
export const missingOperation = (error: unknown) => error instanceof ApiError &&
  error.status === 409 && error.message === "plugin operation not found";
