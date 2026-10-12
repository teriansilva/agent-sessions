import { useEffect, useState, type ReactNode } from "react";
import { Link, useBlocker } from "react-router-dom";
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
    <section className={styles.panel} aria-label="Gallery source">
      <h2>Gallery source</h2>
      <p>
        {value.title ?? value.id} · imported version {value.version} ·{" "}
        {value.publisher}
      </p>
      <p>
        {revision === value.base_revision
          ? "Matches the imported base."
          : "Locally modified. Your edits are independent of upstream content."}
      </p>
      <div className={styles.actions}>
        <a
          className={buttons.ghost}
          href={value.source}
          target="_blank"
          rel="noreferrer"
        >
          Source
        </a>
        <Link className={buttons.ghost} to={galleryComparePath(kind, id)}>
          Compare gallery updates
        </Link>
      </div>
      <details>
        <summary>Source evidence</summary>
        <p>Original content SHA-256</p>
        <code>{value.entry_digest}</code>
        <p>Imported local base</p>
        <code>{value.base_revision}</code>
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
          <p role="status">
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
      <h2>Check the previous operation</h2>
      <p>Reloading does not repeat an import or update.</p>
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
          <button
            className={buttons.ghost}
            onClick={() => {
              if (
                window.confirm(
                  "Stop tracking this checked attempt? This does not cancel server work. Inspect your library and retained copies before starting another operation.",
                )
              ) {
                forgetOperation(id);
                setId(pendingId());
              }
            }}
          >
            Close checked attempt
          </button>
        )}
      </div>
    </section>
  );
}
