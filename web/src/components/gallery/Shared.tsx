import { useEffect, useState, type ReactNode } from "react";
import { Link, useBlocker } from "react-router-dom";
import {
  ChevronRight,
  ExternalLink,
  GalleryHorizontal,
  GitCompareArrows,
} from "lucide-react";
import { ApiError, api } from "../../lib/api";
import { galleryComparePath } from "../../lib/routes";
import type {
  GalleryFiles,
  GalleryKind,
  GalleryOperation,
  GalleryOrigin,
} from "../../types/gallery";
import { PlaybookDialog } from "../playbooks/PlaybookDialog";
import buttons from "../ui/actionButton.module.css";
import styles from "./gallery.module.css";
import {
  localPath,
  errorText,
  pendingId,
  finishOperation,
  forgetOperation,
} from "./operations";

export function FileText({
  value,
}: {
  value: GalleryFiles[string] | undefined;
}) {
  return (
    <pre>
      {value === undefined
        ? "File absent"
        : typeof value === "string"
          ? value
          : `Binary file (${value.base64.length} base64 characters)`}
    </pre>
  );
}
/** One ruled row per file. The summary's text is the file name alone (plus an optional state), so
 * a name stays findable on its own; the chevron is the only open/closed cue. */
export function FileRow({
  name,
  state,
  open,
  children,
}: {
  name: string;
  state?: ReactNode;
  open?: boolean;
  children: ReactNode;
}) {
  return (
    <details className={styles.file} open={open}>
      <summary>
        <ChevronRight size={16} aria-hidden />
        <span>{name}</span>
        {state && <span className={styles.state}>{state}</span>}
      </summary>
      <div className={styles.fileBody}>{children}</div>
    </details>
  );
}
export function Origin({
  value,
  kind,
  id,
  revision,
}: {
  value?: GalleryOrigin | null;
  kind: GalleryKind;
  id: string;
  revision?: string | null;
}) {
  if (!value) return null;
  if (value.state === "unavailable")
    return (
      <p className={styles.error} role="alert">
        Source evidence unavailable: {value.error}
      </p>
    );
  return (
    <section className={styles.origin} aria-label="Gallery source">
      <div className={styles.tag}>
        <GalleryHorizontal size={14} aria-hidden />
        From the gallery // v{value.version} // {value.publisher}
      </div>
      <p>
        {value.title ?? value.id} ·{" "}
        {revision === value.base_revision
          ? "unchanged since import."
          : "edited here. Your edits are independent of the gallery."}
      </p>
      <div className={styles.actions}>
        <a
          className={buttons.ghost}
          href={value.source}
          target="_blank"
          rel="noreferrer"
        >
          <ExternalLink size={14} aria-hidden />
          Source
        </a>
        <Link className={buttons.ghost} to={galleryComparePath(kind, id)}>
          <GitCompareArrows size={14} aria-hidden />
          Compare gallery updates
        </Link>
      </div>
      <details className={styles.file}>
        <summary>
          <ChevronRight size={16} aria-hidden />
          <span>Source evidence</span>
        </summary>
        <div className={styles.fileBody}>
          <p>Original content SHA-256</p>
          <code>{value.entry_digest}</code>
          <p>Imported local base</p>
          <code>{value.base_revision}</code>
        </div>
      </details>
    </section>
  );
}
export function LeaveGuard({ dirty }: { dirty: boolean }) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  return blocker.state === "blocked" ? (
    <PlaybookDialog
      tag="Unsaved content"
      title="Leave this draft?"
      confirmLabel="Discard and leave"
      onCancel={() => blocker.reset()}
      onConfirm={() => blocker.proceed()}
    >
      <p>Your unsaved edits stay here until you choose to leave.</p>
    </PlaybookDialog>
  ) : null;
}
export function PendingOperation({ children }: { children?: ReactNode }) {
  const [id, setId] = useState(pendingId);
  const [notice, setNotice] = useState("");
  const [checked, setChecked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState(false);
  const [result, setResult] = useState<GalleryOperation | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => setId(pendingId()), 700);
    return () => clearInterval(timer);
  }, []);
  if (!id)
    return (
      <>
        {children}
        {result?.state === "complete" && result.result && (
          <p role="status" className={styles.done}>
            The recorded operation completed.{" "}
            <Link to={localPath(result.result.kind, result.result.id)}>
              Open local copy
            </Link>
            {result.result.durable === false
              ? ` ${result.result.durable_reason}`
              : ""}
          </p>
        )}
      </>
    );
  return (
    <section className={styles.recovery} aria-label="Pending gallery operation">
      <h2>
        <span className="hud-led attention" aria-hidden />
        Check the previous operation
      </h2>
      <p>
        The last import or update has no recorded result in this browser.
        Check its status before you start another one. Reloading does not
        repeat an import or update.
      </p>
      <code>{id}</code>
      <p role="status">{notice}</p>
      <div className={styles.actions}>
        <button
          className={buttons.ghost}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const value = await api.galleryOperation(id);
              setResult(value);
              finishOperation(value);
              setId(pendingId());
              setNotice(value.message ?? "Operation found.");
              setChecked(id);
            } catch (error) {
              setNotice(errorText(error));
              setChecked(
                error instanceof ApiError && error.status === 404 ? id : null,
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          Check operation status
        </button>
        {checked === id && (
          <button className={buttons.ghost} onClick={() => setClosing(true)}>
            Close checked attempt
          </button>
        )}
      </div>
      {closing && (
        <PlaybookDialog
          tag="Gallery // operation"
          title="Stop tracking this attempt?"
          confirmLabel="Stop tracking"
          onCancel={() => setClosing(false)}
          onConfirm={() => {
            forgetOperation(id);
            setClosing(false);
            setId(pendingId());
          }}
        >
          <p>
            This does not cancel server work. Inspect your library and retained
            copies before starting another operation.
          </p>
        </PlaybookDialog>
      )}
    </section>
  );
}
