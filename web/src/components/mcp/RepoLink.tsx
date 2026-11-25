/** A server from a repository link (#2353): what the wizard's Review shows while the link is read,
 *  when it is refused, and once it resolved; and the display-only origin on the server page.
 *
 *  The words are the server's (`mcp_gitsource`): the commit is the default branch's head when it was
 *  read, the registry's repository record is a consistency check, and the release installed is the
 *  PUBLISHED one — "not built from this commit" is said wherever the origin is. */
import { Fragment } from "react";
import { Check, Info, Link2, X } from "lucide-react";

import type { McpOrigin, McpResolution, McpResolveProgress, McpLinkFacts, McpSource } from "../../types/mcp";
import { Loader } from "../hud/Loader";
import { declaredWords, linkDisplay, originSaid, refusalHint, resolveRows, sentence } from "./repoLinkModel";
import styles from "./mcp.module.css";

export const DISCLAIMER = "Installs the published release; not built from this commit.";

const LED = { done: "up", running: "", waiting: "idle" } as const;

/** Reading the link: a polled operation, stoppable. Nothing is registered or downloaded meanwhile. */
export function ResolveProgress({
  link,
  progress,
  onStop,
}: {
  link: string;
  progress: McpResolveProgress;
  onStop: () => void;
}) {
  return (
    <>
      <Loader kind="steps" label={`Reading ${linkDisplay(link)}…`} />
      <ol className={styles.checks} aria-label="Reading the link" data-testid="mcp-resolve-rows">
        {resolveRows(progress).map((r, i) => (
          <li key={r.key} data-state={r.state}>
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
                "reading…"
              ) : (
                "waiting"
              )}
            </span>
          </li>
        ))}
      </ol>
      <p className={styles.note} data-testid="mcp-op-note">
        No server is registered and no package is downloaded while the link is read. On a busy host this can take a
        minute.
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
 *  was registered. What was read before the refusal is listed, and Try another link goes back. */
export function LinkRefusal({
  error,
  details,
  onRetry,
}: {
  error: string;
  details: McpLinkFacts | null | undefined;
  onRetry: () => void;
}) {
  const hint = refusalHint(details, error);
  const found = details?.found ?? [];
  return (
    <>
      <div className={`${styles.alert} ${styles.alertWarn}`} role="alert" data-testid="mcp-link-refused">
        <h2>
          <span className="hud-led att" aria-hidden="true" />
          Can’t add this repository
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
            Try another link
          </button>
        </div>
      </div>
      {hint && <p className={styles.note}>{hint}</p>}
    </>
  );
}

/** Where a resolved link's package comes from, the disclaimer, then the label over P1b's facts. */
export function OriginFacts({ resolution }: { resolution: McpResolution }) {
  const { facts, origin } = resolution;
  const d = facts.declared;
  const field = facts.release?.record_field ?? "repository";
  return (
    <div className={styles.origin} data-testid="mcp-review-origin">
      <p className="nj-label">From a repository</p>
      <dl className={styles.kv}>
        <dt>Repository</dt>
        <dd className={styles.mono}>{originSaid(origin)}</dd>
        <dt>Commit</dt>
        <dd>
          <span className={styles.mono}>{origin.commit}</span>
          <span>default branch head when read</span>
        </dd>
        {d && (
          <>
            <dt>Declares</dt>
            <dd>
              <span>
                <span className={styles.mono}>{d.manifest}</span> → {d.kind === "npm" ? "npm" : "PyPI"}{" "}
                <span className={styles.mono}>{d.package}</span> {d.version}
              </span>
            </dd>
          </>
        )}
        <dt>Registry record</dt>
        <dd>
          <span>
            its <span className={styles.mono}>{field}</span> names{" "}
            <span className={styles.mono}>{facts.release?.names ?? facts.repository}</span> — the link’s host, owner and
            repository
          </span>
        </dd>
      </dl>
      <div className={styles.caution} role="note" data-testid="mcp-origin-disclaimer">
        <Info size={18} aria-hidden="true" />
        <div className={styles.disclaimer}>{resolution.disclaimer || DISCLAIMER}</div>
      </div>
      <p className="nj-label">The published release</p>
    </div>
  );
}

/** The server page's Settings → Source: the origin, display only, beside the unchanged source. */
export function OriginNote({ origin, source }: { origin: McpOrigin; source: McpSource }) {
  return (
    <div className={styles.origin} data-testid="mcp-source-origin">
      <dl className={styles.kv}>
        {source.kind !== "remote" && (
          <>
            <dt>Package</dt>
            <dd>
              <span>
                <span className={styles.mono}>{source.package}</span> · {source.kind === "npm" ? "npm" : "PyPI"}
              </span>
            </dd>
          </>
        )}
        <dt>Origin</dt>
        <dd>
          <span>
            From a repository · <span className={styles.mono}>{originSaid(origin)}</span>
          </span>
        </dd>
      </dl>
      <p className={styles.note}>
        Display only: the repository this package was added from. {DISCLAIMER} Saving a different source — kind,
        package or version — removes this line.
      </p>
    </div>
  );
}
