/** A server from a repository link (#2353): what the wizard's Review shows while the link is read,
 *  when it is refused, and once it resolved; and the display-only origin on the server page.
 *
 *  The words are the server's (`mcp_gitsource`): the commit is the default branch's head when it was
 *  read, the registry's repository record is a consistency check, and the release installed is the
 *  PUBLISHED one — "not built from this commit" is said wherever the origin is. A Go module (#2427)
 *  is the exception: it IS built from the commit, and its identity is the module proxy's commit for
 *  it equalling the link's — the server's own disclaimer says so, and the Go block replaces the
 *  release block. Update from source (#2427) shows the same panels old → new. */
import { Fragment, type ReactNode } from "react";
import { Check, Info, Link2, X } from "lucide-react";

import type {
  McpLinkFacts,
  McpOrigin,
  McpPrevious,
  McpResolveProgress,
  McpReviewFacts,
  McpSource,
} from "../../types/mcp";
import { Loader } from "../hud/Loader";
import {
  declaredWords,
  GO_DISCLAIMER,
  GO_TOOLCHAIN_FIRST,
  goCommandWhere,
  goDependencyWords,
  goVersionWords,
  isGoRelease,
  isPackageRelease,
  linkDisplay,
  originSaid,
  refusalHint,
  resolveRows,
  sentence,
  toolchainWords,
} from "./repoLinkModel";
import styles from "./mcp.module.css";

export const DISCLAIMER = "Installs the published release; not built from this commit.";
export { GO_DISCLAIMER };

const LED = { done: "up", running: "", waiting: "idle" } as const;

/** `old → new` when they differ, else just the value. `full` shows the whole new value below. */
export function Change({ old, now, short = (v: string) => v }: { old?: string; now: string; short?: (v: string) => string }) {
  if (!old || old === now) return <span className={styles.mono}>{short(now)}</span>;
  return (
    <span className={styles.change} data-testid="mcp-change">
      <span className={`${styles.mono} ${styles.old}`}>{short(old)}</span>
      <span aria-label="becomes"> → </span>
      <b className={styles.mono}>{short(now)}</b>
    </span>
  );
}

/** Reading the link: a polled operation, stoppable. Nothing is registered or built meanwhile. */
export function ResolveProgress({
  link,
  progress,
  onStop,
  installedCommit,
  note,
}: {
  link: string;
  progress: McpResolveProgress;
  onStop: () => void;
  /** Update from source: the commit the server was added from, beside the head just read. */
  installedCommit?: string;
  /** Replaces the note under the rows. */
  note?: ReactNode;
}) {
  const go = progress.kind === "go";
  return (
    <>
      <Loader kind="steps" label={`Reading ${linkDisplay(link)}…`} />
      <ol className={styles.checks} aria-label="Reading the link" data-testid="mcp-resolve-rows" data-kind={progress.kind ?? ""}>
        {resolveRows(progress, installedCommit).map((r, i) => (
          <li key={r.key} data-state={r.state} data-row={r.key}>
            <span className={`hud-led ${LED[r.state]}`} aria-hidden="true" />
            <span className={styles.n}>{String(i + 1).padStart(2, "0")}</span>
            <span className={styles.label}>{r.label}</span>
            <span className={styles.what}>{r.what}</span>
            <span className={`${styles.res} ${r.state === "done" ? "" : styles.resQuiet}`}>
              {r.state === "done" ? (
                <>
                  <Check size={12} aria-hidden="true" /> done
                </>
              ) : r.state === "running" ? (
                go && r.key === "release" ? "downloading…" : "reading…"
              ) : (
                "waiting"
              )}
            </span>
          </li>
        ))}
      </ol>
      <p className={styles.note} data-testid="mcp-op-note">
        {note ??
          (go
            ? `No server is registered and nothing is built while the link is read. ${GO_TOOLCHAIN_FIRST}: the first Go link downloads BattleLab’s own Go toolchain once; on a slow connection that takes a few minutes.`
            : "No server is registered and no package is downloaded while the link is read. On a busy host this can take a minute.")}
      </p>
      <div className={styles.btnRow}>
        <button type="button" className={styles.ghost} onClick={onStop} data-testid="mcp-resolve-stop">
          <X size={14} aria-hidden="true" />
          Stop reading
        </button>
      </div>
    </>
  );
}

/** A link that can't be installed safely, in words — amber, never red: nothing failed and nothing
 *  was registered. What was read before the refusal is listed, and the retry action goes back. */
export function LinkRefusal({
  error,
  details,
  onRetry,
  title = "Can’t add this repository",
  retryLabel = "Try another link",
}: {
  error: string;
  details: McpLinkFacts | null | undefined;
  onRetry: () => void;
  title?: string;
  retryLabel?: string;
}) {
  const hint = refusalHint(details, error);
  const found = details?.found ?? [];
  return (
    <>
      <div className={`${styles.alert} ${styles.alertWarn}`} role="alert" data-testid="mcp-link-refused">
        <h2>
          <span className="hud-led att" aria-hidden="true" />
          {title}
        </h2>
        <p>{sentence(error)}</p>
        {details?.repository && (
          <dl className={styles.kv}>
            <dt>Read</dt>
            <dd className={styles.mono}>
              {details.repository}
              {details.commit ? ` @ ${details.commit.slice(0, 7)}` : ""}
            </dd>
            {found.map((d, i) => (
              <Fragment key={d.manifest}>
                <dt>{i === 0 ? "Found" : ""}</dt>
                <dd>
                  <span>
                    <span className={styles.mono}>{d.manifest}</span> → {declaredWords(d)}
                  </span>
                </dd>
              </Fragment>
            ))}
          </dl>
        )}
        <div className={styles.btnRow}>
          <button type="button" className={styles.ghost} onClick={onRetry} data-testid="mcp-link-retry">
            <Link2 size={14} aria-hidden="true" />
            {retryLabel}
          </button>
        </div>
      </div>
      {hint && <p className={styles.note}>{hint}</p>}
    </>
  );
}

/** Where a resolved link's package comes from, the server's disclaimer (the Go build line for a Go
 *  module), then the label over the facts below it. `previous` (Update from source) shows the commit
 *  old → new and names the link as the one the server was added from. */
export function OriginFacts({
  resolution,
  previous,
}: {
  resolution: { facts: McpLinkFacts; origin: McpOrigin; disclaimer: string; source: McpSource };
  previous?: McpPrevious;
}) {
  const { facts, origin } = resolution;
  const d = facts.declared;
  const go = resolution.source.kind === "go";
  const release = facts.release;
  return (
    <div className={styles.origin} data-testid="mcp-review-origin">
      <p className="nj-label">From a repository</p>
      <dl className={styles.kv}>
        <dt>Repository</dt>
        <dd>
          {previous ? (
            <>
              <span className={styles.mono}>{linkDisplay(origin.url)}</span>
              <span>the link this server was added from</span>
            </>
          ) : (
            <span className={styles.mono}>{originSaid(origin)}</span>
          )}
        </dd>
        <dt>Commit</dt>
        <dd className={styles.stack}>
          {previous && previous.origin.commit !== origin.commit ? (
            <Change old={previous.origin.commit} now={origin.commit} short={(c) => c.slice(0, 7)} />
          ) : null}
          <span className={styles.mono}>{origin.commit}</span>
          <span>default branch head when read</span>
        </dd>
        {d && (
          <>
            <dt>Declares</dt>
            <dd>
              {d.kind === "go" ? (
                <span>
                  <span className={styles.mono}>{d.manifest}</span> → Go module <span className={styles.mono}>{d.package}</span>
                </span>
              ) : (
                <span>
                  <span className={styles.mono}>{d.manifest}</span> → {d.kind === "npm" ? "npm" : "PyPI"}{" "}
                  <span className={styles.mono}>{d.package}</span> {d.version}
                </span>
              )}
            </dd>
          </>
        )}
        {!go && isPackageRelease(release) && (
          <>
            <dt>Registry record</dt>
            <dd>
              <span>
                its <span className={styles.mono}>{release.record_field || "repository"}</span> names{" "}
                <span className={styles.mono}>{release.names ?? facts.repository}</span> — the link’s host, owner and
                repository
              </span>
            </dd>
          </>
        )}
      </dl>
      <div className={styles.caution} role="note" data-testid="mcp-origin-disclaimer">
        <Info size={18} aria-hidden="true" />
        <div className={styles.disclaimer}>{resolution.disclaimer || (go ? GO_DISCLAIMER : DISCLAIMER)}</div>
      </div>
      <p className="nj-label">
        {go ? "The Go module" : "The published release"}
        {previous ? " · old → new" : ""}
      </p>
    </div>
  );
}

/** A Go module's facts (#2427): the module, its version (the tag at this commit or a pseudo-version),
 *  its home and the mirror note, the command it builds, the Go it needs, the module hash, the
 *  dependencies in its build and the toolchain that builds it. `previous` shows version and hash
 *  old → new (Update from source). */
export function GoFacts({
  review,
  facts,
  previous,
}: {
  review: McpReviewFacts;
  facts?: McpLinkFacts | null;
  previous?: { version?: string; integrity?: string };
}) {
  const src = review.source;
  if (src.kind !== "go") return null;
  const release = isGoRelease(facts?.release) ? facts!.release : undefined;
  const needs = review.go || release?.go || "";
  return (
    <dl className={styles.kv} data-testid="mcp-review-facts" data-kind="go">
      <dt>Module</dt>
      <dd className={styles.mono}>{src.package}</dd>
      <dt>Version</dt>
      <dd>
        <span>
          <Change old={previous?.version} now={src.version} />
          {release ? ` · ${goVersionWords(release)}` : ""}
        </span>
      </dd>
      {release?.home && (
        <>
          <dt>Home</dt>
          <dd className={styles.stack}>
            <span className={styles.mono}>{release.home}</span>
            <span data-testid="mcp-go-home-note">
              {release.mirror
                ? "the module’s own repository · same commit as the link (the link is a mirror)"
                : "the module’s own repository · the link itself"}
            </span>
          </dd>
        </>
      )}
      <dt>Command</dt>
      <dd>
        <span>
          <span className={styles.mono}>{review.entrypoint}</span> · {goCommandWhere(review.command, src.package)}
        </span>
      </dd>
      {needs && (
        <>
          <dt>Needs</dt>
          <dd>Go {needs}</dd>
        </>
      )}
      <dt>Module hash</dt>
      <dd>
        <span>
          <Change
            old={previous?.integrity}
            now={review.integrity}
            short={previous?.integrity && previous.integrity !== review.integrity ? (h) => `${h.slice(0, 10)}…` : undefined}
          />{" "}
          · verified against sum.golang.org
        </span>
      </dd>
      <dt>Dependencies</dt>
      <dd data-testid="mcp-go-deps">{goDependencyWords(review.dependencies)}</dd>
      <dt>Toolchain</dt>
      <dd>
        <span className={styles.mono}>{toolchainWords(review.toolchain)}</span>
      </dd>
    </dl>
  );
}

/** The server page's Settings → Source: the source and the origin, display only. A Go module's
 *  source is never typed in, so its note says how it changes: Update from source. */
export function OriginNote({ origin, source }: { origin: McpOrigin; source: McpSource }) {
  const go = source.kind === "go";
  return (
    <div className={styles.origin} data-testid="mcp-source-origin">
      <dl className={styles.kv}>
        {go ? (
          <>
            <dt>Source</dt>
            <dd>
              <span>
                go · <span className={styles.mono}>{source.package}</span>{" "}
                <span className={styles.mono}>{source.version}</span>
              </span>
            </dd>
          </>
        ) : source.kind !== "remote" ? (
          <>
            <dt>Package</dt>
            <dd>
              <span>
                <span className={styles.mono}>{source.package}</span> · {source.kind === "npm" ? "npm" : "PyPI"}
              </span>
            </dd>
          </>
        ) : null}
        <dt>Origin</dt>
        <dd>
          <span>
            From a repository · <span className={styles.mono}>{originSaid(origin)}</span>
          </span>
        </dd>
      </dl>
      {go ? (
        <p className={styles.note}>
          Display only: the repository this module was built from. {GO_DISCLAIMER} A Go module’s version is never typed
          in: <b>Update from source</b> reads the link again and shows what changes.
        </p>
      ) : (
        <p className={styles.note}>
          Display only: the repository this package was added from. {DISCLAIMER} Saving a different source — kind,
          package or version — removes this line.
        </p>
      )}
    </div>
  );
}
