/** One artifact's page (#1932 P2, board 3, decision 6): a full page at `/library/artifacts/<id>`, the
 *  stable deep link HQ's answers carry.
 *
 *  **The preview is the hosted page itself in a sandboxed frame.** The frame loads the operator's
 *  stable link (`/artifacts/<id>/`); the server checks the sign-in and redirects to a short-lived
 *  view token, and every hosted response carries `Content-Security-Policy: sandbox` without
 *  `allow-same-origin`. The frame repeats that sandbox on the client — scripts, forms, popups and
 *  downloads, NEVER `allow-same-origin` — and sends no referrer, so agent-written HTML is an opaque
 *  origin that cannot read BattleLab's cookies or any `/api` answer even if a header were lost.
 *
 *  Sharing is the deliberate step of `ShareDialogs.tsx`; revoking ends the link on the next request. */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ChevronLeft,
  Copy,
  ExternalLink,
  File,
  FileImage,
  Folder,
  Globe,
  Lock,
  MoreHorizontal,
  ShieldCheck,
  Ban,
  Trash2,
} from "lucide-react";

import { useConfig } from "../app/config";
import { ApiError, api } from "../lib/api";
import { appUrl } from "../lib/appBase";
import { sessionPathFromKey } from "../lib/format";
import { missionLink } from "../lib/missionLink";
import { ARTIFACTS_PATH, hostedArtifactPath } from "../lib/routes";
import type { Artifact, ArtifactDetail as Detail } from "../types/artifacts";
import { Led } from "../components/hud/Meters";
import { Loader } from "../components/hud/Loader";
import { RowMenu, type RowMenuEntry } from "../components/sidebar/RowMenu";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { KindTag, VisibilityTag } from "../components/artifacts/ArtifactBits";
import {
  PublicOffDialog,
  SharePubliclyDialog,
} from "../components/artifacts/ShareDialogs";
import { useArtifactNames } from "../components/artifacts/useArtifactNames";
import {
  absoluteLink,
  ago,
  breakAtHyphens,
  filesLabel,
  formatSize,
  PREVIEW_SANDBOX,
  publicLinksOff,
  revLabel,
} from "../components/artifacts/artifactModel";
import styles from "../components/artifacts/artifacts.module.css";

/** The artifact id shape the server mints (`artifact_store.ID_RE`): checked before any request. */
const ID_RE = /^[1-9][0-9]{3}(?:-[1-9][0-9]{3}){3}$/;
const FILES_SHOWN = 6;

const when = (epoch: number) =>
  new Date(epoch * 1000).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
const message = (e: unknown) =>
  e instanceof Error ? e.message : "The request failed. Try again.";

type Dialog =
  | { kind: "share"; returnTo: HTMLElement | null }
  | { kind: "off"; reason?: string; returnTo: HTMLElement | null }
  | { kind: "revoke"; returnTo: HTMLElement | null }
  | { kind: "delete"; returnTo: HTMLElement | null };

export default function ArtifactDetail() {
  const { artifactId = "" } = useParams();
  const navigate = useNavigate();
  const config = useConfig();
  const valid = ID_RE.test(artifactId);
  const [a, setA] = useState<Detail | null>(null);
  const [missing, setMissing] = useState(!valid);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [pinned, setPinned] = useState<number | null>(null);
  const [allFiles, setAllFiles] = useState(false);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [busy, setBusy] = useState(false);
  const names = useArtifactNames(a ? [a.origin.session_key] : []);

  const load = useCallback(() => {
    if (!valid) return Promise.resolve();
    return api.artifact(artifactId).then(
      (next) => {
        setA(next);
        setError("");
      },
      (e: unknown) => {
        if (e instanceof ApiError && e.status === 404) setMissing(true);
        else setError(message(e));
      },
    );
  }, [artifactId, valid]);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);

  if (missing)
    return (
      <div className={styles.page} data-testid="artifact-missing">
        <div className={styles.wrap}>
          <Link className={styles.back} to={ARTIFACTS_PATH}>
            <ChevronLeft size={14} aria-hidden="true" />
            Artifacts
          </Link>
          <h1 className={styles.h2}>This artifact isn&apos;t here</h1>
          <p className={styles.note}>
            It was deleted, or the link is not an artifact&apos;s.
          </p>
        </div>
      </div>
    );
  if (!a)
    return (
      <div className={styles.page}>
        {error ? (
          <p className={styles.error} role="alert">
            {error}
          </p>
        ) : (
          <Loader kind="acquire" label="Acquiring the artifact" />
        )}
      </div>
    );

  const latest = a.revisions[0];
  const files = latest?.files ?? a.files?.length ?? 0;
  const bytes = latest?.bytes ?? 0;
  const origin = window.location.origin;
  const privateLink = absoluteLink(a.url, origin);
  const shown = pinned ?? null;
  const previewPath = hostedArtifactPath(a.id, shown ?? undefined);
  const previewLink = absoluteLink(previewPath, origin);
  const offHere = publicLinksOff(config?.auth_mode);
  const sessionPath = sessionPathFromKey(a.origin.session_key);
  const sessionTitle = names.session(a.origin.session_key);
  const projectName = names.project(a.origin.project_id);
  const missionTitle = names.mission(a.origin.mission_id);
  const firstRev = a.revisions[a.revisions.length - 1];

  const merge = (next: Artifact) =>
    setA((cur) =>
      cur
        ? { ...cur, ...next, files: cur.files, revisions: cur.revisions }
        : cur,
    );
  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setStatus(`${what} copied.`);
    } catch {
      setStatus("Copying needs a secure origin; select the link instead.");
    }
  };
  const openShare = (returnTo: HTMLElement | null) =>
    setDialog(
      offHere ? { kind: "off", returnTo } : { kind: "share", returnTo },
    );

  const act = async () => {
    if (!dialog || busy) return;
    setBusy(true);
    try {
      if (dialog.kind === "revoke") {
        merge(await api.revokeArtifact(a.id));
        setStatus("The public link is ended.");
        setDialog(null);
      } else if (dialog.kind === "delete") {
        await api.deleteArtifact(a.id);
        navigate(ARTIFACTS_PATH, { replace: true });
      }
    } catch (e) {
      setStatus(message(e));
      setDialog(null);
    } finally {
      setBusy(false);
    }
  };

  const menu: RowMenuEntry[] = [
    {
      key: "copy-private",
      label: "Copy private link",
      icon: <Copy size={15} />,
      onSelect: () => void copy(privateLink, "Private link"),
    },
    ...(a.visibility === "public"
      ? [
          {
            key: "revoke",
            label: "Revoke public link",
            icon: <Ban size={15} />,
            danger: true,
            onSelect: () =>
              setDialog({
                kind: "revoke",
                returnTo: document.activeElement as HTMLElement | null,
              }),
          },
        ]
      : []),
    {
      key: "delete",
      label: "Delete…",
      icon: <Trash2 size={15} />,
      danger: true,
      onSelect: () =>
        setDialog({
          kind: "delete",
          returnTo: document.activeElement as HTMLElement | null,
        }),
    },
  ];
  const fileRows = a.files ?? [];

  return (
    <div className={styles.page} data-testid="artifact-detail">
      <div className={styles.wrap}>
        <div className={styles.dtop}>
          <div className={styles.dtitle}>
            <Link className={styles.back} to={ARTIFACTS_PATH}>
              <ChevronLeft size={14} aria-hidden="true" />
              Artifacts
            </Link>
            <div className={styles.tagRow}>
              <KindTag kind={a.kind} entry={a.entry} />
              <VisibilityTag visibility={a.visibility} />
              <span className={styles.meta}>
                <b>{revLabel(a.revision)}</b>
                <span className={styles.dot}>latest · {ago(a.updated_at)}</span>
                <span className={styles.dot}>
                  {filesLabel(files)} · {formatSize(bytes)}
                </span>
              </span>
            </div>
            <h1 className={styles.h2}>{a.title}</h1>
            <p className={styles.whyLine}>
              {a.why}{" "}
              <span className={styles.agentFlag}>Written by the agent</span>
            </p>
          </div>
          <div className={styles.dacts}>
            <a
              className={styles.ghost}
              href={appUrl(hostedArtifactPath(a.id))}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={14} aria-hidden="true" />
              Open
            </a>
            {a.visibility === "public" && a.public_url ? (
              <button
                type="button"
                className={styles.ghost}
                onClick={() =>
                  void copy(absoluteLink(a.public_url!, origin), "Public link")
                }
              >
                <Copy size={14} aria-hidden="true" />
                Copy public link
              </button>
            ) : (
              <button
                type="button"
                className={offHere ? styles.ghost : styles.primary}
                onClick={(e) => openShare(e.currentTarget)}
                data-testid="artifact-share"
              >
                <Globe size={14} aria-hidden="true" />
                Share publicly…
              </button>
            )}
            <RowMenu
              items={menu}
              title={a.title}
              sheetTitle="Artifact actions"
              triggerLabel="More for this artifact"
              triggerIcon={<MoreHorizontal size={16} aria-hidden="true" />}
              triggerClassName={styles.iconBtn}
            />
          </div>
        </div>

        <div className={styles.detail}>
          <section className={styles.browser} aria-label="Preview">
            <div className={styles.bbar}>
              <div className={styles.addr} title={previewLink}>
                <Lock size={12} aria-hidden="true" />
                <span>{previewLink}</span>
              </div>
              <button
                type="button"
                className={styles.rowIcon}
                aria-label="Copy this link"
                onClick={() => void copy(previewLink, "Link")}
              >
                <Copy size={14} aria-hidden="true" />
              </button>
              <a
                className={styles.rowIcon}
                href={appUrl(previewPath)}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Open in a new tab"
              >
                <ExternalLink size={14} aria-hidden="true" />
              </a>
            </div>
            <div className={styles.bstrip}>
              <span>
                <ShieldCheck size={12} aria-hidden="true" />
                <b>Sandboxed</b> · no cookies, no access to BattleLab
              </span>
              <span>
                Showing{" "}
                <b>
                  {shown ? (
                    <span className={styles.rlabel}>{revLabel(shown)}</span>
                  ) : (
                    <>
                      latest (
                      <span className={styles.rlabel}>
                        {revLabel(a.revision)}
                      </span>
                      )
                    </>
                  )}
                </b>
              </span>
              {a.entry && <span>/{a.entry}</span>}
            </div>
            <iframe
              key={previewPath}
              className={styles.frame}
              src={appUrl(previewPath)}
              title={`Preview of ${a.title}`}
              sandbox={PREVIEW_SANDBOX}
              referrerPolicy="no-referrer"
              data-testid="artifact-preview"
            />
          </section>

          <div className={styles.side}>
            <section
              className={`${styles.section} ${styles.shareSection}`}
              aria-label="Sharing"
              data-testid="artifact-sharing"
            >
              <h2 className={styles.secHead}>
                Sharing
                {a.visibility === "public" ? (
                  <span className={styles.live}>
                    <Led tone="up" />
                    Public link live
                  </span>
                ) : (
                  <span>Private</span>
                )}
              </h2>
              {a.visibility === "public" && a.public_url ? (
                <>
                  <div className={styles.linkRow}>
                    <span
                      className={styles.linkField}
                      data-testid="artifact-public-url"
                    >
                      {absoluteLink(a.public_url, origin)}
                    </span>
                    <button
                      type="button"
                      className={styles.ghost}
                      onClick={() =>
                        void copy(
                          absoluteLink(a.public_url!, origin),
                          "Public link",
                        )
                      }
                    >
                      <Copy size={14} aria-hidden="true" />
                      Copy
                    </button>
                  </div>
                  <p className={styles.note}>
                    {a.public_since ? `Shared ${when(a.public_since)}. ` : ""}
                    Anyone with this link can open it, no sign-in. It serves the
                    latest revision.
                  </p>
                  <div>
                    <button
                      type="button"
                      className={styles.dangerBtn}
                      onClick={(e) =>
                        setDialog({ kind: "revoke", returnTo: e.currentTarget })
                      }
                      data-testid="artifact-revoke"
                    >
                      <Ban size={14} aria-hidden="true" />
                      Revoke public link
                    </button>
                  </div>
                  <p className={styles.note}>
                    Revoking ends the link on the next request. Sharing again
                    makes a new link; the old one never comes back.
                  </p>
                </>
              ) : (
                <>
                  <div className={styles.linkRow}>
                    <span className={styles.linkField}>
                      {breakAtHyphens(privateLink)}
                    </span>
                    <button
                      type="button"
                      className={styles.ghost}
                      onClick={() => void copy(privateLink, "Private link")}
                    >
                      <Copy size={14} aria-hidden="true" />
                      Copy
                    </button>
                  </div>
                  {offHere ? (
                    <div className={styles.offBox}>
                      <Ban size={14} aria-hidden="true" />
                      <p>
                        <b>Public links are off on this box.</b> It runs without
                        sign-in (AUTH_MODE=none), so BattleLab serves no public
                        routes.
                      </p>
                    </div>
                  ) : (
                    <p className={styles.note}>
                      Only you, signed in, can open this link.
                    </p>
                  )}
                </>
              )}
            </section>

            <section className={styles.section} aria-label="Revisions">
              <h2 className={styles.secHead}>
                Revisions
                <span>{a.revisions.length} · same link</span>
              </h2>
              <ul className={styles.revs}>
                {a.revisions.map((r, i) => (
                  <li
                    key={r.n}
                    className={`${styles.rev} ${i === 0 ? styles.revLatest : ""}`}
                  >
                    <b>{revLabel(r.n)}</b>
                    <span className={styles.revMeta}>
                      {ago(r.created_at)} · {formatSize(r.bytes)} ·{" "}
                      {filesLabel(r.files)}
                    </span>
                    {i === 0 ? (
                      shown === null ? (
                        <span className={styles.latest}>Latest</span>
                      ) : (
                        <button
                          type="button"
                          className={styles.pin}
                          onClick={() => setPinned(null)}
                        >
                          Latest
                        </button>
                      )
                    ) : (
                      <button
                        type="button"
                        className={styles.pin}
                        aria-pressed={shown === r.n}
                        aria-label={`Show ${revLabel(r.n)} in the preview`}
                        onClick={() => setPinned(r.n)}
                      >
                        @{r.n}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
              <p className={styles.note}>
                <span className={styles.mono}>/artifacts/&lt;id&gt;/</span> is
                the stable link and always shows the latest;{" "}
                <span className={styles.mono}>&lt;id&gt;@&lt;n&gt;</span> pins
                one revision, best-effort (pruned after 20 newer revisions).
              </p>
            </section>

            <section className={styles.section} aria-label="Where it came from">
              <h2 className={styles.secHead}>Where it came from</h2>
              <dl className={styles.kv}>
                <dt>Session</dt>
                <dd className={styles.provTop}>
                  <span className={styles.eng}>{a.origin.engine}</span>
                  {sessionPath ? (
                    <Link
                      className={`${styles.link} ${styles.ell}`}
                      to={sessionPath}
                    >
                      {sessionTitle ?? a.origin.session_key}
                    </Link>
                  ) : (
                    <span className={styles.ell}>{a.origin.session_key}</span>
                  )}
                </dd>
                {a.origin.project_id && (
                  <>
                    <dt>Project</dt>
                    <dd className={styles.provSub}>
                      <Folder size={12} aria-hidden="true" />
                      {projectName ?? a.origin.project_id}
                    </dd>
                  </>
                )}
                <dt>Folder</dt>
                <dd className={styles.mono}>{a.origin.cwd}</dd>
                {a.origin.mission_id && (
                  <>
                    <dt>Mission</dt>
                    <dd>
                      <Link
                        className={styles.link}
                        to={missionLink(a.origin.mission_id)}
                      >
                        {missionTitle ?? a.origin.mission_id}
                      </Link>
                    </dd>
                  </>
                )}
                <dt>Published</dt>
                <dd className={styles.mono}>
                  {when(a.updated_at)}
                  {firstRev && a.revisions.length > 1
                    ? ` (${revLabel(firstRev.n)} ${when(firstRev.created_at)})`
                    : ""}
                </dd>
              </dl>
            </section>

            {fileRows.length > 0 && (
              <section className={styles.section} aria-label="Files">
                <h2 className={styles.secHead}>
                  Files
                  <span>
                    {fileRows.length} · {formatSize(bytes)}
                  </span>
                </h2>
                <ul className={styles.files}>
                  {(allFiles ? fileRows : fileRows.slice(0, FILES_SHOWN)).map(
                    (f) => (
                      <li key={f.path} className={styles.file}>
                        {f.type?.startsWith("image/") ? (
                          <FileImage size={14} aria-hidden="true" />
                        ) : (
                          <File size={14} aria-hidden="true" />
                        )}
                        <span className={styles.ell} title={f.path}>
                          {f.path}
                        </span>
                        <span className={styles.fs}>{formatSize(f.size)}</span>
                      </li>
                    ),
                  )}
                </ul>
                {fileRows.length > FILES_SHOWN && (
                  <button
                    type="button"
                    className={styles.textBtn}
                    onClick={() => setAllFiles((v) => !v)}
                  >
                    {allFiles
                      ? "Show fewer"
                      : `Show all ${fileRows.length} files`}
                  </button>
                )}
              </section>
            )}
          </div>
        </div>
        <p className={styles.status} role="status">
          {status}
        </p>
      </div>

      {dialog?.kind === "share" && (
        <SharePubliclyDialog
          artifact={a}
          files={files}
          bytes={bytes}
          returnFocusTo={dialog.returnTo}
          onCancel={() => setDialog(null)}
          onShared={(next) => {
            merge(next);
            setDialog(null);
            setStatus("Shared. The public link is live.");
          }}
          onPublicOff={(reason) =>
            setDialog({ kind: "off", reason, returnTo: dialog.returnTo })
          }
        />
      )}
      {dialog?.kind === "off" && (
        <PublicOffDialog
          artifact={a}
          reason={dialog.reason}
          returnFocusTo={dialog.returnTo}
          onClose={() => setDialog(null)}
        />
      )}
      {(dialog?.kind === "revoke" || dialog?.kind === "delete") && (
        <ConfirmDialog
          tag={
            dialog.kind === "revoke" ? "Revoke public link" : "Delete artifact"
          }
          title={
            dialog.kind === "revoke"
              ? `End the public link to “${a.title}”?`
              : `Delete “${a.title}”?`
          }
          confirmLabel={dialog.kind === "revoke" ? "Revoke" : "Delete"}
          danger={dialog.kind === "delete"}
          busy={busy}
          onCancel={() => setDialog(null)}
          onConfirm={() => void act()}
          returnFocusTo={dialog.returnTo}
          fallbackFocusSelector="[data-testid='artifact-detail'] h1"
          markers={{
            confirm: { "data-testid": `artifact-${dialog.kind}-confirm` },
          }}
        >
          <p className={styles.note}>
            {dialog.kind === "revoke"
              ? "Revoking ends the link on the next request. Sharing again makes a new link; the old one never comes back."
              : "Every revision and its files are removed, and both links stop working. The session's own files are not touched."}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
