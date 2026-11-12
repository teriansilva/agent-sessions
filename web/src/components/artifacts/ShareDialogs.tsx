/** Sharing an artifact publicly (#1932 P2, board 3): the deliberate step, and the box that cannot.
 *
 *  - **Share publicly** is the shared ConfirmDialog (focus on Cancel). It says in plain words what a
 *    public link means, and its confirm stays off until "I checked the files" is ticked
 *    (decision 5). The link is minted by the server on confirm; nothing here guesses it.
 *  - **Public links are off** (`AUTH_MODE=none`): the server serves no public routes, so the dialog
 *    says why, keeps the private link one tap away and points at Security settings. It opens either
 *    from the config's `auth_mode` before any request, or from the server's 409 `public_off`. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Ban, Copy, ShieldCheck, TriangleAlert } from "lucide-react";

import { api } from "../../lib/api";
import { settingsPath } from "../../routes/settingsTabs";
import type { Artifact, ArtifactDetail } from "../../types/artifacts";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { ArtifactDialog } from "./ArtifactDialog";
import {
  absoluteLink,
  breakAtHyphens,
  filesLabel,
  formatSize,
  isPublicOff,
  kindLabel,
} from "./artifactModel";
import styles from "./artifacts.module.css";

export function SharePubliclyDialog({
  artifact,
  files,
  bytes,
  onCancel,
  onShared,
  onPublicOff,
  returnFocusTo,
}: {
  artifact: Artifact | ArtifactDetail;
  files: number;
  bytes: number;
  onCancel: () => void;
  onShared: (updated: Artifact) => void;
  /** The server said public links are off here (409 `public_off`), with its reason. */
  onPublicOff: (reason: string) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const origin = window.location.origin;
  const confirm = async () => {
    setBusy(true);
    setError("");
    try {
      onShared(await api.shareArtifact(artifact.id));
    } catch (e) {
      if (isPublicOff(e)) {
        const rec = (e as { record?: { reason?: string } }).record;
        onPublicOff(rec?.reason ?? "");
        return;
      }
      setError(e instanceof Error ? e.message : "Sharing failed. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <ConfirmDialog
      tag={`Share publicly // ${kindLabel(artifact.kind, artifact.entry)}`}
      title={`Share “${artifact.title}” publicly?`}
      confirmLabel={busy ? "Sharing…" : "Share publicly"}
      confirmDisabled={!checked}
      busy={busy}
      onCancel={onCancel}
      onConfirm={() => void confirm()}
      returnFocusTo={returnFocusTo}
      fallbackFocusSelector="[data-testid='artifact-share']"
      markers={{
        dialog: { "data-testid": "artifact-share-dialog" },
        confirm: { "data-testid": "artifact-share-confirm" },
      }}
    >
      <div className={styles.warn}>
        <TriangleAlert size={16} aria-hidden="true" />
        <p>
          <b>Anyone with the link can open it.</b> No sign-in, no expiry. Search
          engines are asked not to index it, but a link that gets passed on
          cannot be taken back, only revoked.
        </p>
        <ul className={styles.bullets}>
          <li>
            <span className={styles.sq} aria-hidden="true" />
            <span>
              The link shows the latest revision, and every later one the agent
              publishes.
            </span>
          </li>
          <li>
            <span className={styles.sq} aria-hidden="true" />
            <span>
              It is served from this BattleLab, so it works while this box is
              reachable at its public address.
            </span>
          </li>
          <li>
            <span className={styles.sq} aria-hidden="true" />
            <span>
              The page stays sandboxed: it cannot reach BattleLab or your
              sign-in.
            </span>
          </li>
        </ul>
      </div>
      <div className={styles.target}>
        <span className={styles.lbl}>Public link</span>
        <span className={styles.mono}>
          {absoluteLink("/artifacts/p/•••••••••••/", origin)}
        </span>
        <span className={styles.note}>
          A new random link is made when you confirm. Your private link stays as
          it is.
        </span>
      </div>
      <label className={styles.check}>
        <input
          type="checkbox"
          checked={checked}
          disabled={busy}
          onChange={(e) => setChecked(e.target.checked)}
          data-testid="artifact-share-checked"
        />
        <span>
          I checked the files: {filesLabel(files)}, {formatSize(bytes)}. Nothing
          in them should stay private.
        </span>
      </label>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </ConfirmDialog>
  );
}

export function PublicOffDialog({
  artifact,
  reason,
  onClose,
  returnFocusTo,
}: {
  artifact: Artifact | ArtifactDetail;
  /** The server's own words, when it sent them (409 `public_off`). */
  reason?: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [copied, setCopied] = useState("");
  const link = absoluteLink(artifact.url, window.location.origin);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied("Private link copied.");
    } catch {
      setCopied("Copying needs a secure origin; select the link instead.");
    }
  };
  return (
    <ArtifactDialog
      tag="Share publicly"
      value="unavailable"
      title="Public links are off on this box"
      onClose={onClose}
      returnFocusTo={returnFocusTo}
      testId="artifact-public-off"
      actions={
        <>
          <Link
            to={settingsPath("security")}
            className={styles.ghost}
            onClick={onClose}
          >
            <ShieldCheck size={14} aria-hidden="true" />
            Security settings
          </Link>
          <button
            type="button"
            className={styles.ghost}
            onClick={() => void copy()}
          >
            <Copy size={14} aria-hidden="true" />
            Copy private link
          </button>
          <button type="button" className={styles.ghost} onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      <div className={styles.offBox}>
        <Ban size={14} aria-hidden="true" />
        <p>
          This BattleLab runs <b>without sign-in</b> (
          <span className={styles.mono}>AUTH_MODE=none</span>). Anyone who
          reaches it is already treated as you, so it serves{" "}
          <b>no public routes</b>: a public link would answer 404.
        </p>
      </div>
      {reason && <p className={`${styles.note} ${styles.spaced}`}>{reason}</p>}
      <p className={`${styles.note} ${styles.spaced}`}>
        Your private link keeps working for you. To share with someone else,
        turn sign-in on, then share from here.
      </p>
      <div className={styles.target}>
        <span className={styles.lbl}>Private link</span>
        <span className={styles.mono}>{breakAtHyphens(link)}</span>
      </div>
      <p className={styles.status} role="status">
        {copied}
      </p>
    </ArtifactDialog>
  );
}
