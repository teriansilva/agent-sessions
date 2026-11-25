/** A server from a repository link (#2353): the pure parts the wizard, the server page and HQ's card
 *  share. The server decides everything that matters (`mcp_gitsource`); these only say it. */
import type { McpDeclared, McpLinkFacts, McpOrigin, McpResolveProgress } from "../../types/mcp";
import { suggestName } from "./mcpModel";

/** `https://host/owner/repo`, an optional `.git` and trailing `/`. Nothing else is a root link. */
const ROOT = /^https:\/\/[^/\s?#@]+\/[^/\s?#]+\/[^/\s?#]+?(?:\.git)?\/?$/i;

/** Why a typed link can't be read, before asking the server ("" when it looks like a root link).
 *  The server checks again; this only saves a round trip for the obvious cases. */
export function linkError(raw: string): string {
  const url = raw.trim();
  if (!url) return "The repository’s link, e.g. https://github.com/owner/repo.";
  if (!/^https:\/\//i.test(url)) return "An https:// link. ssh, git:// and http links are refused.";
  if (/\/(?:-\/)?(?:tree|blob|src)\//.test(url)) return "Link the repository itself — no /tree/ or /blob/ links.";
  if (!ROOT.test(url)) return "A repository’s root link: https://host/owner/repo.";
  return "";
}

/** The comparison form of a link: host lowercased, `.git` and trailing `/` dropped. */
export function normalizeLink(raw: string): string {
  let url = raw.trim();
  for (;;) {
    const next = url.replace(/\/+$/, "").replace(/\.git$/i, "");
    if (next === url) break;
    url = next;
  }
  const m = /^(https?):\/\/([^/]+)(.*)$/i.exec(url);
  return m ? `${m[1].toLowerCase()}://${m[2].toLowerCase()}${m[3]}` : url;
}

/** `host/owner/repo` of a link (what the panels show). */
export function linkDisplay(raw: string): string {
  return normalizeLink(raw).replace(/^https?:\/\//i, "");
}

/** `github.com/owner/repo @ 3f2a9c1`. */
export function originSaid(origin: McpOrigin): string {
  return `${linkDisplay(origin.url)} @ ${origin.commit.slice(0, 7)}`;
}

/** A name for the server from the repository's own name, like the package kinds' suggestion. */
export function nameFromLink(raw: string): string {
  if (linkError(raw)) return "";
  const repo = normalizeLink(raw).split("/").pop() ?? "";
  return suggestName({ kind: "npm", package: repo, version: "" });
}

export const declaredWords = (d: McpDeclared) =>
  `${d.kind === "npm" ? "npm" : "PyPI"} ${d.package || "(no name)"} ${d.version || (d.dynamic ? "(dynamic version)" : "(no version)")}`;

export type RowState = "done" | "running" | "waiting";
export interface ResolveRow {
  key: "commit" | "manifest" | "release" | "repository" | "review";
  label: string;
  what: string;
  state: RowState;
}

const ORDER: ResolveRow["key"][] = ["commit", "manifest", "release", "repository", "review"];

/** The progress rows of a running resolution: everything before its step done, its step running,
 *  the rest waiting. What each row says fills in as the facts arrive. */
export function resolveRows(p: McpResolveProgress): ResolveRow[] {
  const at = ORDER.indexOf(p.step ?? "commit");
  const pkg = p.package ? `${p.kind === "pypi" ? "PyPI" : "npm"} ${p.package} ${p.version ?? ""}`.trim() : "";
  const what: Record<ResolveRow["key"], string> = {
    commit: p.commit ? `default branch head ${p.commit.slice(0, 7)}` : "the default branch’s head commit",
    manifest: p.manifest && p.package ? `${p.manifest} → ${p.package} ${p.version ?? ""}`.trim() : "package.json or pyproject.toml at the root",
    release: pkg ? `${pkg}, exactly` : "the published release at that version",
    repository: "the release’s repository record against the link",
    review: "the exact version into a lock (nothing installed)",
  };
  const labels: Record<ResolveRow["key"], string> = {
    commit: "Commit",
    manifest: "Manifest",
    release: "Release",
    repository: "Repository",
    review: "Lock",
  };
  return ORDER.map((key, i) => ({
    key,
    label: labels[key],
    what: what[key],
    state: i < at ? "done" : i === at ? "running" : "waiting",
  }));
}

/** What a refusal offers instead, when there is something better to say than "try another link". */
export function refusalHint(details: McpLinkFacts | null | undefined, error: string): string {
  if ((details?.found?.length ?? 0) > 1)
    return "To install one of them, choose npm package or PyPI package on Source and give its name and exact version.";
  if (/no published release/i.test(error))
    return "BattleLab installs only what the registry publishes. Once this version is released, read the link again.";
  return "";
}

/** A refusal's words as a sentence ("npm" keeps its own lowercase spelling). */
export function sentence(text: string): string {
  const t = text.trim();
  if (!t) return "";
  const s = /^npm\b/.test(t) ? t : t[0].toUpperCase() + t.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}
