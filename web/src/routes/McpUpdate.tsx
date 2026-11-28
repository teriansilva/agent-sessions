/** Library → MCP servers → one server → Update from source (#2427): a repository-link server's OWN
 *  link read again, at its default branch's head, on the app's one wizard shell — Source · Review ·
 *  Check.
 *
 *  - **Reading** is the same polled, stoppable operation as adding a link (the server takes the link
 *    from its record, never from here). Nothing changes meanwhile.
 *  - **Review** shows what changes, old → new (version, commit, module hash or release integrity),
 *    with the same facts as adding it — or "Already up to date at <version> (<commit>)" and nothing
 *    to apply.
 *  - **Install** applies the reading (`PATCH {resolution_id}`: the server replaces source and origin
 *    together, fenced to what was read), then installs exactly the reviewed `review_id` (polled),
 *    then lands on the server's Projects tab: the new install needs its own passing check. A 409
 *    (the server changed since its link was read) offers Update again; a 404 (the reading is gone)
 *    reads the link again.
 *  - **The hint can be ahead of the head** (#2431): the "newer version" hint reads the registry,
 *    this reads the repository's default branch. When they differ, Review says so and still names
 *    the version it would actually install (`lagWords`). */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { ApiError, api } from "../lib/api";
import { MCP_ID_RE, MCP_PATH, mcpServerPath } from "../lib/routes";
import type {
  McpLinkFacts,
  McpResolveProgress,
  McpServerDetail,
  McpServerSummary,
  McpUpdateResolution,
} from "../types/mcp";
import { WizardShell, type WizardStep } from "../components/wizard/WizardShell";
import { Callsign, Panel } from "../components/hud/Panel";
import { Loader } from "../components/hud/Loader";
import { Caution, ReviewFacts, WhatHappensNext } from "../components/mcp/McpBits";
import { OperationEnded, useMcpOperation } from "../components/mcp/mcpOperation";
import { Change, GoFacts, LinkRefusal, OriginFacts, ResolveProgress } from "../components/mcp/RepoLink";
import {
  installedIntegrity,
  linkDisplay,
  updatedFlash,
  updateSteps,
  upToDateWords,
} from "../components/mcp/repoLinkModel";
import { errorText } from "../components/mcp/mcpData";
import { when } from "../components/mcp/mcpModel";
import { lagWords } from "../components/mcp/updateModel";
import styles from "../components/mcp/mcp.module.css";

const STEPS: WizardStep[] = [
  { id: "source", label: "Source" },
  { id: "review", label: "Review" },
  { id: "check", label: "Check" },
];

type Phase = "loading" | "reading" | "refused" | "review" | "uptodate" | "conflict";

export default function McpUpdate() {
  const { serverId = "" } = useParams();
  return <UpdatePage key={serverId} serverId={serverId} />;
}

function UpdatePage({ serverId }: { serverId: string }) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<McpServerDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [phase, setPhase] = useState<Phase>("loading");
  const [progress, setProgress] = useState<McpResolveProgress>({});
  const [result, setResult] = useState<McpUpdateResolution | null>(null);
  const [refusal, setRefusal] = useState<{ error: string; details: McpLinkFacts | null } | null>(null);
  const [conflict, setConflict] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Set once the update is applied: a failed install retries the install only, never the PATCH.
  const [applied, setApplied] = useState<McpServerSummary | null>(null);
  const resolveOp = useMcpOperation();
  const installOp = useMcpOperation();
  const readSeq = useRef(0);
  // The read still running, if any: leaving the page stops it (the server holds one read per server).
  const running = useRef("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (running.current) void api.stopMcpOperation(running.current).catch(() => undefined);
    };
  }, []);

  /** Read the server, then its link (an operation the page polls); a newer read or Stop drops an
   *  older one's answer. */
  const read = useCallback(async () => {
    const seq = ++readSeq.current;
    let d: McpServerDetail;
    try {
      // The server as it is NOW: its link, and the `updated_at` an apply is fenced to.
      d = await api.mcpServer(serverId);
    } catch (e) {
      if (mounted.current && seq === readSeq.current)
        setLoadError((e as { status?: number }).status === 404 ? "This MCP server isn’t here. It may have been removed." : errorText(e));
      return;
    }
    if (!mounted.current || seq !== readSeq.current) return;
    setResult(null);
    setRefusal(null);
    setConflict("");
    setError("");
    setApplied(null);
    setProgress({});
    setDetail(d);
    setLoadError("");
    if (!d.server.origin) return; // said below: only a repository-link server updates from source
    setPhase("reading");
    try {
      const res = await resolveOp(
        `update:${serverId}:${seq}`,
        (rid) => {
          running.current = rid;
          return api.resolveMcpServerLink(serverId, rid);
        },
        (op) => {
          if (seq === readSeq.current) setProgress(op.progress ?? {});
        },
      );
      running.current = "";
      if (!res || seq !== readSeq.current) return;
      setResult(res);
      setPhase(res.up_to_date ? "uptodate" : "review");
    } catch (e) {
      running.current = "";
      if (seq !== readSeq.current) return;
      setRefusal({ error: errorText(e), details: e instanceof OperationEnded ? (e.op?.details ?? null) : null });
      setPhase("refused");
    }
  }, [serverId, resolveOp]);

  useEffect(() => {
    // Every setState in `read` follows an await (the server's read): nothing cascades.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (MCP_ID_RE.test(serverId)) void read();
  }, [serverId, read]);

  const back = mcpServerPath(serverId);
  const s = detail?.server;
  const name = s ? s.label || s.name : "";

  const stop = () => {
    readSeq.current += 1;
    const rid = running.current;
    running.current = "";
    if (rid) void api.stopMcpOperation(rid).catch(() => undefined);
    navigate(`${back}?tab=settings`, { state: { flash: "Stopped reading the link. Nothing changed." } });
  };

  /** Apply the reading (source and origin together, server-held), then install exactly the review
   *  it showed. A failed install keeps the applied source: Install again retries the install. */
  const install = async () => {
    if (!s || !result || result.up_to_date) return;
    setBusy(true);
    setError("");
    setNote("");
    try {
      let target = applied;
      if (!target) {
        try {
          target = await api.applyMcpUpdate(s.id, result.resolution_id, s.updated_at);
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            setConflict(errorText(e));
            setPhase("conflict");
            return;
          }
          if (e instanceof ApiError && e.status === 404) {
            setNote("The link’s reading was no longer held (it expired, or BattleLab restarted), so it was read again. Review it and press Install.");
            void read();
            return;
          }
          throw e;
        }
        setApplied(target);
      }
      const sid = target.id;
      const rv = result.review.review_id;
      const done = await installOp(`${sid}:${rv}`, (rid) => api.installMcpServer(sid, rv, rid));
      if (!done) return;
      navigate(`${back}?tab=projects`, { state: { flash: updatedFlash(result.source) } });
    } catch (e) {
      setError(errorText(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  if (!MCP_ID_RE.test(serverId) || loadError)
    return (
      <div className={styles.page} data-testid="mcp-update-missing">
        <div className={styles.wrap}>
          <Link className={styles.back} to={MCP_ID_RE.test(serverId) ? back : MCP_PATH}>
            <ArrowLeft size={14} aria-hidden="true" /> {MCP_ID_RE.test(serverId) ? "The server" : "MCP servers"}
          </Link>
          <div className={styles.alert} role="alert">
            <h2>Couldn’t read this MCP server</h2>
            <p>{loadError || "This MCP server isn’t here. It may have been removed."}</p>
            {MCP_ID_RE.test(serverId) && (
              <button type="button" className={styles.ghost} onClick={() => void read()}>
                <RefreshCw size={14} aria-hidden="true" /> Retry
              </button>
            )}
          </div>
        </div>
      </div>
    );

  const kind = result?.source.kind ?? progress.kind ?? s?.source.kind ?? "";
  const go = kind === "go";
  const previous = result?.previous;
  const oldIntegrity = s && previous ? installedIntegrity(s.installed, previous.source) : "";
  const prevVersion = previous && previous.source.kind !== "remote" ? previous.source.version : "";
  const target = result && result.source.kind !== "remote" ? result.source.version : "";
  const lag = s && result ? lagWords(target, s.update, result.up_to_date) : null;
  const lagLine = lag && (
    <p className={styles.note} role="note" data-testid="mcp-update-lag">
      {lag}
    </p>
  );

  const body = (() => {
    if (!s) return <Loader kind="skeleton" label="Acquiring the server" rows={4} />;
    if (!s.origin)
      return (
        <Panel rail="short" faceClassName={styles.face}>
          <Callsign title="Update from source" value={s.name} />
          <p className={styles.note} role="status" data-testid="mcp-update-no-origin">
            {s.name} wasn’t added from a repository link, so there is no source to read again. Change its version on
            its Settings tab.
          </p>
        </Panel>
      );
    if (phase === "loading" || phase === "reading")
      return (
        <Panel rail="short" faceClassName={styles.face}>
          <Callsign title="What changes" value={linkDisplay(s.origin.url)} />
          {note && (
            <p className={styles.note} role="status">
              {note}
            </p>
          )}
          <ResolveProgress
            link={s.origin.url}
            progress={progress}
            onStop={stop}
            installedCommit={s.origin.commit}
            note={`Nothing changes while the link is read: ${s.name} keeps ${
              s.source.kind === "remote" ? "what it has" : s.source.version
            } (${s.origin.commit.slice(0, 7)}) and every session keeps getting it.`}
          />
        </Panel>
      );
    if (phase === "refused" && refusal)
      return (
        <Panel rail="short" faceClassName={styles.face}>
          <Callsign title="What changes" value={linkDisplay(s.origin.url)} />
          <LinkRefusal
            error={refusal.error}
            details={refusal.details}
            onRetry={() => void read()}
            title="Can’t update from this link"
            retryLabel="Read the link again"
          />
          <p className={styles.note}>Nothing changed: {s.name} keeps what it has, and its check still counts.</p>
        </Panel>
      );
    if (phase === "conflict")
      return (
        <Panel rail="short" faceClassName={styles.face}>
          <Callsign title="What changes" value={s.name} />
          <div className={`${styles.alert} ${styles.alertWarn}`} role="alert" data-testid="mcp-update-conflict">
            <h2>
              <span className="hud-led att" aria-hidden="true" />
              The server changed
            </h2>
            <p>
              {s.name} changed after its link was read (someone saved it, or another update ran first), so nothing was
              updated. Update again to read the link against what it is now.
            </p>
            {conflict && <p className={styles.note}>The server said: {conflict}</p>}
          </div>
        </Panel>
      );
    if (phase === "uptodate" && result?.up_to_date)
      return (
        <Panel rail="short" faceClassName={styles.face}>
          <Callsign title="Up to date" value={s.name} />
          <p className={styles.verdict} role="status" data-testid="mcp-update-current">
            <span className="hud-led up" aria-hidden="true" />
            <b>{upToDateWords(result.source, result.origin)}</b>
          </p>
          {lagLine}
          <dl className={styles.kv}>
            <dt>Repository</dt>
            <dd className={styles.mono}>{linkDisplay(result.origin.url)}</dd>
            <dt>Read</dt>
            <dd>
              <span>
                default branch head <span className={styles.mono}>{result.origin.commit.slice(0, 7)}</span>
              </span>
            </dd>
            {s.source.kind !== "remote" && (
              <>
                <dt>Installed</dt>
                <dd>
                  <span>
                    <span className={styles.mono}>
                      {s.source.package} {s.source.version}
                    </span>{" "}
                    · {go ? "built" : "installed"} from {s.origin.commit.slice(0, 7)}
                    {s.installed?.installed_at ? ` · ${when(s.installed.installed_at)}` : ""}
                  </span>
                </dd>
              </>
            )}
          </dl>
          <p className={styles.note}>
            The link’s head is the commit already installed. Nothing was {go ? "built" : "installed"} and nothing
            changed; the server’s check still counts.
          </p>
        </Panel>
      );
    if (result && !result.up_to_date) {
      const pkg = result.source.kind !== "remote" ? result.source : null;
      return (
        <Panel rail="short" faceClassName={styles.face} data-testid="mcp-update-review">
          <Callsign title="What changes" value={s.name} />
          {note && (
            <p className={styles.note} role="status">
              {note}
            </p>
          )}
          {lagLine}
          <OriginFacts resolution={result} previous={previous} />
          {go ? (
            <GoFacts review={result.review} facts={result.facts} previous={{ version: prevVersion, integrity: oldIntegrity }} />
          ) : (
            <>
              <dl className={styles.kv} data-testid="mcp-update-package">
                <dt>Version</dt>
                <dd>
                  <Change old={prevVersion} now={pkg?.version ?? ""} />
                </dd>
                <dt>Integrity</dt>
                <dd>
                  <Change
                    old={oldIntegrity}
                    now={result.review.integrity}
                    short={oldIntegrity && oldIntegrity !== result.review.integrity ? (h) => `${h.slice(0, 14)}…` : undefined}
                  />
                </dd>
              </dl>
              <ReviewFacts facts={result.review} />
            </>
          )}
          <Caution title="Third-party code">
            It runs as your user inside every session that gets this server. Review before installing.
          </Caution>
          {applied && error ? (
            <div className={styles.alert} role="alert" data-testid="mcp-update-install-failed">
              <h2>Updated, but not installed</h2>
              <p>
                {s.name} now names {pkg?.version}, but {go ? "building" : "installing"} it failed: {error}
              </p>
              <p className={styles.note}>
                New sessions don’t get {s.name} until it is installed and a check passes. Install again, or update from
                source again later.
              </p>
            </div>
          ) : busy ? (
            <p className={styles.note} role="status" data-testid="mcp-op-note">
              {go
                ? "Applying the update and building exactly this module version. A build can take a few minutes; it carries on even if you leave this page."
                : "Applying the update and installing exactly this lock. A package can take a few minutes; the install carries on even if you leave this page."}
            </p>
          ) : (
            <p className={styles.note}>
              Nothing has changed yet: Install {go ? "builds" : "fetches"} exactly {pkg?.version}
              {go ? " at this hash" : ""} and replaces {prevVersion || "the current version"} in one step. The new{" "}
              {go ? "build" : "install"} needs its own passing check.
            </p>
          )}
        </Panel>
      );
    }
    return null;
  })();

  const upToDate = phase === "uptodate";
  const onNext =
    !s || !s.origin || phase === "refused"
      ? undefined
      : upToDate
        ? () => navigate(back)
        : phase === "conflict"
          ? () => void read()
          : () => void install();
  const nextLabel = upToDate
    ? `Back to ${name}`
    : phase === "conflict"
      ? "Update again"
      : busy
        ? "Installing…"
        : applied && error
          ? "Install again"
          : "Install";

  return (
    <WizardShell
      kicker={s ? `Update ${s.name} from source` : "Update from source"}
      steps={STEPS}
      current={1}
      wide
      heading={upToDate ? "Nothing to update" : "What changes"}
      onNext={onNext}
      nextLabel={nextLabel}
      nextDisabled={busy || (!upToDate && phase !== "conflict" && phase !== "review")}
      leaveGuard={false}
      leaveTitle="Leave the update?"
      secondary={
        upToDate ? undefined : (
          <Link className={styles.ghost} to={back}>
            Cancel
          </Link>
        )
      }
    >
      <div className={styles.stepGrid} data-testid="mcp-update" data-phase={phase}>
        <div className={styles.valueCell}>
          {body}
          {error && !applied && (
            <p className={styles.error} role="alert" data-testid="mcp-wizard-error">
              {error}
            </p>
          )}
        </div>
        <Panel rail="short" faceClassName={styles.face} className={styles.aside}>
          <Callsign title="What happens next" />
          <WhatHappensNext
            current={1}
            items={s?.origin ? updateSteps(s.name, s.origin.url, kind, upToDate) : undefined}
          />
        </Panel>
      </div>
    </WizardShell>
  );
}
