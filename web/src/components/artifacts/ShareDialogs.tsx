/** Sharing an artifact publicly (#1932 P2, board 3): the deliberate step, and the box that cannot.
 *
 *  - **Share publicly** is the shared ConfirmDialog (focus on Cancel). It says in plain words what a
 *    public link means, and its confirm stays off until "I checked the files" is ticked
 *    (decision 5). The link is minted by the server on confirm; nothing here guesses it.
 *  - **On a Home Free box** (`AUTH_MODE=none` with a Home Free agent, #2190) the box serves no public
 *    routes, so a public link is a COPY hosted on the BattleLab relay at its own address. The dialog
 *    says so plainly: the copy leaves this box.
 *  - **Public links are off** (`AUTH_MODE=none`, no relay): the dialog says why, keeps the private
 *    link one tap away and points at Security settings. It opens either from the config before any
 *    request, or from the server's 409 `public_off` (with the relay's reason when it refused).
 *  - **The link's options** (#2217): a password, an expiry and the BattleLab header, set when the
 *    link is made. The defaults are the link as it always was. A relay copy has none of them yet
 *    (#2219), so on a Home Free box they show disabled with the reason. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { Ban, Copy, ShieldCheck, TriangleAlert } from "lucide-react";

import { api } from "../../lib/api";
import {
  EXPIRY_PRESETS,
  expiryFor,
  formatWhen,
  nowSeconds,
  type ExpiryPreset,
} from "../../lib/shareFormat";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  Artifact,
  ArtifactDetail,
  ArtifactShareOptions,
} from "../../types/artifacts";
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
  viaRelay = false,
}: {
  artifact: Artifact | ArtifactDetail;
  files: number;
  bytes: number;
  /** A Home Free box (#2190): the public link is a copy hosted on the BattleLab relay. */
  viaRelay?: boolean;
  onCancel: () => void;
  onShared: (updated: Artifact) => void;
  /** The server said public links are off here (409 `public_off`), with its reason. */
  onPublicOff: (reason: string) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [preset, setPreset] = useState<ExpiryPreset>("never");
  const [picked, setPicked] = useState("");
  const [header, setHeader] = useState(true);
  const [now, setNow] = useState(nowSeconds);
  const origin = window.location.origin;
  const expiresAt = viaRelay ? null : expiryFor(preset, now, picked);
  const badDate =
    typeof expiresAt === "number" &&
    (!Number.isFinite(expiresAt) || expiresAt <= nowSeconds());
  const withPassword = !viaRelay && password !== "";
  const confirm = async () => {
    if (badDate) return;
    setBusy(true);
    setError("");
    // A relay copy (#2219) takes no options: the call stays the option-less one it always was.
    const options: ArtifactShareOptions | undefined = viaRelay
      ? undefined
      : {
          ...(withPassword ? { password } : {}),
          expires_at: expiryFor(preset, nowSeconds(), picked),
          header,
        };
    try {
      onShared(await api.shareArtifact(artifact.id, options));
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
      confirmDisabled={!checked || badDate}
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
        <p data-testid="artifact-share-summary">
          <b>
            {withPassword
              ? "Anyone with the link and the password can open it."
              : "Anyone with the link can open it."}
          </b>{" "}
          No sign-in.{" "}
          {expiresAt == null
            ? "No expiry."
            : badDate
              ? ""
              : `It stops working ${formatWhen(expiresAt)}, or when you revoke it.`}{" "}
          Search engines are asked not to index it, but a link that gets passed
          on cannot be taken back, only revoked.
        </p>
        <ul className={styles.bullets}>
          <li>
            <span className={styles.sq} aria-hidden="true" />
            <span>
              The link shows the latest revision, and every later one the agent
              publishes.
            </span>
          </li>
          {viaRelay ? (
            <li data-testid="artifact-share-relay">
              <span className={styles.sq} aria-hidden="true" />
              <span>
                <b>A copy leaves this box.</b> It is hosted on the BattleLab
                relay at its own address, and stays online while this box keeps
                connecting to the relay (up to 30 days after it last did).
              </span>
            </li>
          ) : (
            <li>
              <span className={styles.sq} aria-hidden="true" />
              <span>
                It is served from this BattleLab, so it works while this box is
                reachable at its public address.
              </span>
            </li>
          )}
          <li>
            <span className={styles.sq} aria-hidden="true" />
            <span>
              The page stays sandboxed: it cannot reach BattleLab or your
              sign-in.
            </span>
          </li>
        </ul>
      </div>
      <fieldset
        className={styles.linkOpts}
        disabled={busy || viaRelay}
        aria-describedby={viaRelay ? "artifact-share-relay-why" : undefined}
        data-testid="artifact-share-options"
      >
        <legend className={styles.lbl}>Link options</legend>
        <span className={styles.optLbl} id="artifact-share-pw-label">
          Password
        </span>
        <div className={styles.optRow}>
          <input
            id="artifact-share-pw"
            aria-labelledby="artifact-share-pw-label"
            className={styles.optInput}
            type={showPw ? "text" : "password"}
            autoComplete="new-password"
            placeholder={viaRelay ? "Not available here" : "Optional"}
            maxLength={256}
            value={viaRelay ? "" : password}
            onChange={(e) => setPassword(e.target.value)}
            data-testid="artifact-share-password"
          />
          <button
            type="button"
            className={styles.ghost}
            aria-pressed={showPw}
            onClick={() => setShowPw((v) => !v)}
          >
            {showPw ? "Hide" : "Show"}
          </button>
        </div>
        {!viaRelay && (
          <p className={styles.note}>
            BattleLab keeps only a hash: tell the visitor yourself, you can’t
            see it again.
          </p>
        )}
        <span className={styles.optLbl} id="artifact-share-expiry">
          Expires
        </span>
        <div
          className={styles.presets}
          role="group"
          aria-labelledby="artifact-share-expiry"
        >
          {(viaRelay
            ? EXPIRY_PRESETS.filter((p) => p.id === "never")
            : EXPIRY_PRESETS
          ).map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={viaRelay ? true : preset === p.id}
              className={styles.preset}
              onClick={() => {
                setPreset(p.id);
                setNow(nowSeconds());
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
        {!viaRelay && preset === "date" && (
          <input
            className={styles.optInput}
            type="datetime-local"
            aria-label="Link ends at"
            value={picked}
            onChange={(e) => setPicked(e.target.value)}
          />
        )}
        {!viaRelay && (
          <p className={styles.note} data-testid="artifact-share-expiry-hint">
            {expiresAt == null
              ? "Never expires. You can revoke it at any time."
              : badDate
                ? "Pick a date and time in the future."
                : `Ends ${formatWhen(expiresAt)}. After that the link answers “This link no longer works”.`}
          </p>
        )}
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={viaRelay ? true : header}
            onChange={(e) => setHeader(e.target.checked)}
            data-testid="artifact-share-header"
          />
          <span>Show the BattleLab header above it</span>
        </label>
        <p
          className={styles.note}
          id={viaRelay ? "artifact-share-relay-why" : undefined}
        >
          {viaRelay
            ? "This box runs without sign-in, so the public copy is hosted on the BattleLab relay, which has no password, expiry or header switch yet. Turn sign-in on to host the link on this box with them."
            : "Off: the artifact fills the whole page, with no BattleLab title bar. It stays sandboxed."}
        </p>
      </fieldset>
      <div className={styles.target}>
        <span className={styles.lbl}>Public link</span>
        <span className={styles.mono}>
          {viaRelay
            ? "https://•••••••••••.artifacts.battlelabos.com/"
            : absoluteLink("/artifacts/p/•••••••••••/", origin)}
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
