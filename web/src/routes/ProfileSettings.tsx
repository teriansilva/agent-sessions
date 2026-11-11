/** Settings › General › Profile (#1931): the operator's avatar.
 *
 *  It shows beside the operator's messages in API sessions and to everyone who opens one of their
 *  API share links, so the hint says so. The image is cropped and scaled here (`squareAvatar`);
 *  the server accepts only PNG/JPEG/WebP bytes under its cap and is the authority on what it
 *  stored — the preview always comes from `/api/config.avatar`, never from the chosen file. */
import { useRef, useState } from "react";

import { useConfig, useConfigReload } from "../app/config";
import { Avatar } from "../components/chat/Avatar";
import { operatorAvatarSpec } from "../components/chat/avatarSpec";
import { ApiError, api } from "../lib/api";
import { AVATAR_ACCEPT, AvatarImageError, squareAvatar } from "../lib/avatarImage";
import styles from "./Settings.module.css";

export function ProfileSettings() {
  const config = useConfig();
  const reload = useConfigReload();
  const input = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState<"upload" | "remove" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const version = config?.avatar ?? null;

  const run = async (kind: "upload" | "remove", fn: () => Promise<unknown>) => {
    setBusy(kind);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(
        e instanceof AvatarImageError || e instanceof ApiError
          ? e.message
          : "That didn’t go through — try again.",
      );
    } finally {
      await reload();
      setBusy(null);
    }
  };

  const chosen = (file: File | undefined) => {
    if (!file) return;
    void run("upload", async () => api.putAvatar(await squareAvatar(file)));
  };

  return (
    <section className={styles.section} aria-labelledby="profile-avatar-h" data-testid="profile-settings">
      <h2 id="profile-avatar-h">Avatar</h2>
      <div className={styles.avatarRow}>
        <Avatar
          spec={operatorAvatarSpec(version, config?.username)}
          className={styles.avatarPreview}
        />
        <div className={styles.avatarActions}>
          <input
            ref={input}
            type="file"
            accept={AVATAR_ACCEPT}
            hidden
            data-testid="profile-avatar-input"
            onChange={(e) => {
              chosen(e.currentTarget.files?.[0]);
              e.currentTarget.value = ""; // choosing the same file again still uploads
            }}
          />
          <button
            type="button"
            className={styles.secBtn}
            disabled={busy !== null}
            onClick={() => input.current?.click()}
          >
            {busy === "upload" ? "Uploading…" : version ? "Replace image" : "Upload image"}
          </button>
          {version && (
            <button
              type="button"
              className={styles.secBtnGhost}
              disabled={busy !== null}
              onClick={() => void run("remove", () => api.deleteAvatar())}
            >
              {busy === "remove" ? "Removing…" : "Remove"}
            </button>
          )}
        </div>
      </div>
      {err && (
        <p className={styles.err} role="alert">
          {err}
        </p>
      )}
      <p className={styles.hint}>
        PNG, JPEG or WebP. It is cropped to a square and scaled to 256 px in your browser. It shows
        beside your messages in API sessions, and to everyone who opens one of your share links.
      </p>
      <p className={styles.hint}>
        Without an image you appear as your initial. Visitors on a share link appear as theirs, in a
        colour picked from their name; the agent appears as{" "}
        <Avatar spec={{ kind: "agent" }} className={styles.avatarInline} />.
      </p>
    </section>
  );
}
