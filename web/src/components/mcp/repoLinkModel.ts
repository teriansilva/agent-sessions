/** A server from a repository link (#2353): the pure parts the wizard, the server page and HQ's card
 *  share. The server decides everything that matters (`mcp_gitsource`); these only say it. */
import type {
  McpDeclared,
  McpGoRelease,
  McpLinkFacts,
  McpOrigin,
  McpPackageRelease,
  McpResolveProgress,
  McpReviewFacts,
  McpToolchain,
} from "../../types/mcp";
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

/** The Go build line (#2427). The server sends its own (`disclaimer`); this is only the fallback. */
export const GO_DISCLAIMER =
  "Built from this commit with the Go toolchain. No install scripts run; every download is verified against sum.golang.org.";

/** What a manifest declares, in words. A Go module's version is the module proxy's (never go.mod's),
 *  so a `go.mod` names only the module. */
export const declaredWords = (d: McpDeclared) =>
  d.kind === "go"
    ? `Go module ${d.package || "(no module path)"}`
    : `${d.kind === "npm" ? "npm" : "PyPI"} ${d.package || "(no name)"} ${d.version || (d.dynamic ? "(dynamic version)" : "(no version)")}`;

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
export function resolveRows(p: McpResolveProgress, installedCommit = ""): ResolveRow[] {
  const rows = p.kind === "go" ? goRows(p) : packageRows(p);
  // Update from source (#2427): the head just read beside the commit the server was added from.
  if (installedCommit && p.commit) rows[0].what += ` · installed ${installedCommit.slice(0, 7)}`;
  return rows;
}

function packageRows(p: McpResolveProgress): ResolveRow[] {
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

/** While the Go module is looked up: its first use also downloads BattleLab's own Go toolchain. */
export const GO_TOOLCHAIN_FIRST = "Downloading the Go toolchain (first time only)";

/** The same five steps for a Go module (#2427): `release` is the module at this commit from the
 *  module proxy (the first Go link also downloads the toolchain then), `repository` the proof that
 *  the proxy's commit for it is the link's own. */
function goRows(p: McpResolveProgress): ResolveRow[] {
  const at = ORDER.indexOf(p.step ?? "commit");
  const what: Record<ResolveRow["key"], string> = {
    commit: p.commit ? `default branch head ${p.commit.slice(0, 7)}` : "the default branch’s head commit",
    manifest: p.package ? `go.mod → ${p.package}` : "go.mod at the root",
    release:
      at === 2
        ? `${GO_TOOLCHAIN_FIRST}, then the module at this commit from the module proxy`
        : p.version
          ? `${p.package ?? "the module"} ${p.version}, from the module proxy`
          : "the module at this commit, from the module proxy, and its home",
    repository: "the proxy’s commit for this version against the link’s commit",
    review: "its module hash, dependencies and command (nothing built)",
  };
  const labels: Record<ResolveRow["key"], string> = {
    commit: "Commit",
    manifest: "Manifest",
    release: "Module",
    repository: "Commit match",
    review: "Review",
  };
  return ORDER.map((key, i) => ({
    key,
    label: labels[key],
    what: what[key],
    state: i < at ? "done" : i === at ? "running" : "waiting",
  }));
}

export const isGoRelease = (r: McpLinkFacts["release"] | undefined): r is McpGoRelease =>
  !!r && typeof (r as McpGoRelease).module === "string";
export const isPackageRelease = (r: McpLinkFacts["release"] | undefined): r is McpPackageRelease =>
  !!r && typeof (r as McpPackageRelease).record_field === "string";

/** "the tag at this commit" when the proxy found a tag pointing at it; else a pseudo-version. */
export function goVersionWords(release: Pick<McpGoRelease, "ref" | "version"> | undefined): string {
  if (release?.ref?.startsWith("refs/tags/")) return "the tag at this commit";
  return "a pseudo-version for this commit (no tag points at it)";
}

/** Where the command a Go review found lives: the module root, or its path under the module. */
export function goCommandWhere(command: string | undefined, module: string): string {
  if (!command || command === module) return "the module root";
  return command.startsWith(`${module}/`) ? command.slice(module.length + 1) : command;
}

/** `go1.27.4 · BattleLab’s own, sha256-verified · cgo off`. */
export function toolchainWords(t: McpToolchain | undefined): string {
  if (!t?.version) return "BattleLab’s own, sha256-verified · cgo off";
  return `go${t.version.replace(/^go/, "")} · BattleLab’s own, sha256-verified · cgo off`;
}

/** The dependency row: the modules in the build `go install` uses, each authenticated. */
export function goDependencyWords(n: number): string {
  return `${n} module${n === 1 ? "" : "s"} in its build · each verified against sum.golang.org`;
}

/** A Go review's facts, or null for an npm / PyPI one. */
export const goReview = (r: McpReviewFacts | null | undefined) => (r && r.source.kind === "go" ? r : null);

/** What a refusal offers instead, when there is something better to say than "try another link". */
export function refusalHint(details: McpLinkFacts | null | undefined, error: string): string {
  const goModule = details?.found?.length === 1 && details.found[0].kind === "go" ? details.found[0].package : "";
  if (/several commands/i.test(error))
    return "BattleLab builds exactly one command per server and never picks one for you. Nothing was built or registered.";
  if (goModule && /module proxy/i.test(error))
    return `A mirror is accepted only when the module’s own repository has the same commit. Link ${goModule} itself, or a commit it has published. Nothing was built or registered.`;
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

// ---- Update from source (#2427) ---------------------------------------------------------------

type Versioned = { kind: string; version?: string };
const versionOf = (s: Versioned | undefined) => (s && "version" in s ? (s.version ?? "") : "");

/** "Already up to date at v3.3.0 (9c3fa38)". */
export function upToDateWords(source: Versioned, origin: McpOrigin): string {
  const v = versionOf(source);
  return `Already up to date at ${v || "its version"} (${origin.commit.slice(0, 7)})`;
}

/** What the server page says once an update is installed: the new install needs its own check. */
export function updatedFlash(source: Versioned): string {
  const v = versionOf(source);
  return `Updated to ${v || "the new version"} — run the checks; new sessions don't get this server until a check passes.`;
}

/** The integrity the server's CURRENT source was installed with (the "old" side of the hash row),
 *  or "" when that source isn't installed (nothing to compare against). */
export function installedIntegrity(
  installed: { integrity?: string; source?: unknown } | null | undefined,
  previous: unknown,
): string {
  if (!installed?.integrity) return "";
  return JSON.stringify(installed.source) === JSON.stringify(previous) ? installed.integrity : "";
}

/** The three steps of Update from source, said for the kind it is (a Go module is BUILT). When the
 *  link is already up to date nothing changes, so Check says the existing check still counts. */
export function updateSteps(name: string, link: string, kind: string, upToDate: boolean): [string, string][] {
  const go = kind === "go";
  return [
    ["Source", `the link ${name} was added from, read again at its default branch’s head. ${linkDisplay(link)}.`],
    [
      "Review",
      go
        ? "what changes, old → new. Install builds exactly the new module version and hash, and replaces the source and origin in one step."
        : "what changes, old → new. Install fetches exactly the new lock, and replaces the source and origin in one step.",
    ],
    [
      "Check",
      upToDate
        ? `nothing to check again: ${name} keeps its install and its passing check.`
        : `the new ${go ? "build" : "install"} needs its own passing check. Until then no new session gets ${name}; settings, credentials and projects are kept.`,
    ],
  ];
}
