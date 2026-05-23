/** Display helpers shared across the sidebar. */

export function relTime(epoch: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function shortCwd(cwd: string): string {
  return cwd.replace(/^\/home\/[^/]+\//, "~/");
}

export function engineBadge(engine: string): string {
  return engine === "opencode" ? "oc" : engine === "codex" ? "cx" : engine === "gemini" ? "gm" : "cc";
}
