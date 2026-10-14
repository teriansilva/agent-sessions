import { useEffect, useState, type ReactNode } from "react";
import { Link, useBlocker } from "react-router-dom";
import { ChevronRight, ExternalLink, GalleryHorizontal } from "lucide-react";
import { ApiError, api } from "../../lib/api";
import { galleryUpdatePath } from "../../lib/routes";
import type {
  GalleryFiles,
  GalleryKind,
  GalleryOperation,
  GalleryOrigin,
} from "../../types/gallery";
import { PlaybookDialog } from "../playbooks/PlaybookDialog";
import buttons from "../ui/actionButton.module.css";
import styles from "./gallery.module.css";
import { updateStatus } from "./galleryRules";
import { galleryWait, useGallery } from "./useGallery";
import {
  localPath,
  errorText,
  finishOperation,
  forgetOperation,
  pendingId,
  pendingLabel,
  usePendingId,
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
/** Every digest, sequence, signature, source and history field lives in ONE collapsed disclosure
 *  (#1440). It may hold IDs; it never holds an action the operator needs to recover. */
export function TechnicalDetails({ children }: { children: ReactNode }) {
  return (
    <details
      className={`${styles.file} ${styles.evidence}`}
      data-testid="technical-details"
    >
      <summary>
        <ChevronRight size={16} aria-hidden />
        <span>Technical details</span>
      </summary>
      <div className={`${styles.fileBody} ${styles.facts}`}>{children}</div>
    </details>
  );
}
/** A label + value row inside Technical details. */
export function Fact({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className={styles.fact}>
      <span>{label}</span>
      <div>{children}</div>
    </div>
  );
}
/** Shown on content the app verified against the installed gallery key. */
export function Verified() {
  return (
    <span className={styles.verified}>
      <span className="hud-led up" aria-hidden />
      Verified
    </span>
  );
}

const ORIGIN_NOTE = {
  unreachable: "Can't check for updates right now.",
  stale:
    "The gallery listing is out of date, so updates are paused until it refreshes.",
  withdrawn:
    "This has been withdrawn from the gallery. Your copy keeps working.",
  damaged:
    "Its gallery history can't be read, so changes are paused until it is restored from a backup.",
} as const;

/** A local copy that came from the gallery: one line, an "Update available" badge when — and only
 *  when — `updateStatus` says so, and the evidence folded into Technical details (#1440). */
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
  const gallery = useGallery();
  if (!value) return null;
  // Only a listing the current check confirmed can say "Update available" (#1440).
  const status = updateStatus(
    value,
    galleryWait(gallery) ? null : gallery.data,
  );
  const damaged = value.state !== "verified";
  const edited = revision !== value.base_revision;
  return (
    <section className={styles.origin} aria-label="Gallery source">
      <p className={styles.originLine}>
        <GalleryHorizontal size={14} aria-hidden />
        <span>
          From the gallery
          {!damaged &&
            ` · v${value.version} · ${edited ? "edited locally" : "unchanged"}`}
        </span>
        {status?.state === "available" && (
          <span className={styles.badge} data-testid="update-available">
            <span className="hud-led attention" aria-hidden />
            Update available
          </span>
        )}
      </p>
      {status && status.state !== "available" && status.state !== "current" && (
        <p
          className={damaged ? styles.error : styles.why}
          role={damaged ? "alert" : undefined}
        >
          {status.state === "unreachable" && gallery.checking
            ? "Checking for updates…"
            : ORIGIN_NOTE[status.state]}
        </p>
      )}
      {status?.state === "available" && (
        <div className={styles.actions}>
          <Link
            className={buttons.primary}
            to={galleryUpdatePath(kind, status.entry.digest, id)}
          >
            Review update to v{status.entry.content.version}
          </Link>
        </div>
      )}
      <TechnicalDetails>
        {damaged ? (
          <Fact label="Problem">
            {value.error ?? "Source history unavailable."}
          </Fact>
        ) : (
          <>
            <Fact label="Gallery entry">
              {value.title ?? value.id} · {value.publisher}
            </Fact>
            {value.source && (
              <Fact label="Source">
                <a href={value.source} target="_blank" rel="noreferrer">
                  <ExternalLink size={12} aria-hidden /> {value.source}
                </a>
              </Fact>
            )}
            <Fact label="Original content SHA-256">
              <code>{value.entry_digest}</code>
            </Fact>
            <Fact label="Imported local base">
              <code>{value.base_revision}</code>
            </Fact>
          </>
        )}
      </TechnicalDetails>
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
/** The uncertain-operation banner (#1440: never inside Technical details). While an add or update
 *  has no recorded outcome in this browser, every gallery write stays disabled (`beginOperation`
 *  refuses) until Check status settles it or the operator explicitly stops tracking it. */
export function PendingOperation({ children }: { children?: ReactNode }) {
  const [id, setId] = usePendingId();
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState("");
  const [checked, setChecked] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState(false);
  const [result, setResult] = useState<GalleryOperation | null>(null);
  if (!id)
    return (
      <>
        {children}
        {result?.state === "complete" && result.result && (
          <p role="status" className={styles.done}>
            It was added.{" "}
            <Link to={localPath(result.result.kind, result.result.id)}>
              Open
            </Link>
            {result.result.durable === false
              ? " The disk didn't confirm the save, so check it before relying on it."
              : ""}
          </p>
        )}
      </>
    );
  const label = pendingLabel(id);
  return (
    <section
      className={styles.recovery}
      aria-label="Pending gallery operation"
      data-testid="gallery-pending-operation"
    >
      <h2>
        <span className="hud-led attention" aria-hidden />
        We couldn't confirm whether {label
          ? `adding ${label}`
          : "the last add"}{" "}
        finished.
      </h2>
      <p>Check before trying again. Reloading never repeats it.</p>
      {notice && <p role="status">{notice}</p>}
      <div className={styles.actions}>
        <button
          className={buttons.primary}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const value = await api.galleryOperation(id);
              setResult(value);
              finishOperation(value);
              setId(pendingId());
              setNotice(
                value.state === "complete"
                  ? ""
                  : "We still can't tell whether it was added. Look in your library before trying again.",
              );
              setDetail(value.message ?? "");
              setChecked(id);
            } catch (error) {
              const missing = error instanceof ApiError && error.status === 404;
              setNotice(
                missing
                  ? "It wasn't recorded as added. If no other tab is still adding it, you can try again."
                  : "The status couldn't be checked. Try again in a moment.",
              );
              setDetail(errorText(error));
              setChecked(missing ? id : null);
            } finally {
              setBusy(false);
            }
          }}
        >
          Check status
        </button>
        {checked === id && (
          <button className={buttons.ghost} onClick={() => setClosing(true)}>
            Try again
          </button>
        )}
      </div>
      <TechnicalDetails>
        <Fact label="Operation">
          <code>{id}</code>
        </Fact>
        {detail && <Fact label="Server answer">{detail}</Fact>}
      </TechnicalDetails>
      {closing && (
        <PlaybookDialog
          tag="Gallery // try again"
          title="Try it again?"
          confirmLabel="Yes, try again"
          onCancel={() => setClosing(false)}
          onConfirm={() => {
            forgetOperation(id);
            setClosing(false);
            setId(pendingId());
            setNotice("");
          }}
        >
          <p>
            This stops waiting for the earlier attempt. It does not cancel work
            still running in another tab, so look in your library before adding
            it again.
          </p>
        </PlaybookDialog>
      )}
    </section>
  );
}
