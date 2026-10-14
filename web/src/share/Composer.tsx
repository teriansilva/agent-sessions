import { Paperclip, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { PublicError, publicApi, type PublicSnapshot, type VisitorShare } from "./publicApi";
import styles from "./ShareViewer.module.css";

/** Pictures per message: the runtime takes at most four attachments on a turn. */
const MAX_IMAGES = 4;
const IMAGE_TYPES = "image/png,image/jpeg,image/gif,image/webp";

interface Pending {
  stored: string;
  preview: string;
  name: string;
}

/** A `send` link's composer (#1404 P4).
 *
 *  Nobody types into a session anonymously: until the visitor names themselves the composer is a
 *  name field (mockup B5), with the note that a name is self-chosen. Then: text, images, Stop for
 *  the running turn, Send. A send that may have landed (network drop, 5xx) keeps its retry key, so
 *  sending the same words again is a no-op on the server rather than a second message. */
export function Composer({
  share,
  csrf,
  snap,
  onShare,
  onSent,
  onRecheck,
  refused,
}: {
  share: VisitorShare;
  csrf: string;
  snap: PublicSnapshot;
  onShare: (share: VisitorShare) => void;
  /** Re-read the view after a send or stop. Never throws: a failed re-read is the poll's job. */
  onSent: () => Promise<void>;
  onRecheck: () => Promise<void>;
  refused: (e: unknown) => boolean;
}) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(share.name ?? "");
  const [text, setText] = useState("");
  const [images, setImages] = useState<Pending[]>([]);
  const [busy, setBusy] = useState<"name" | "send" | "upload" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const retry = useRef<{ key: string; identity: string } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const imagesRef = useRef(images);
  useEffect(() => {
    imagesRef.current = images;
  }, [images]);

  // Object URLs for previews are this page's to free.
  useEffect(() => () => imagesRef.current.forEach((p) => URL.revokeObjectURL(p.preview)), []);

  const fail = (e: unknown, fallback: string) => {
    if (refused(e)) return;
    if (e instanceof PublicError && e.status === 403 && /name/.test(e.message)) {
      onShare({ ...share, name: null, name_required: true });
    }
    if (e instanceof PublicError && e.status === 403 && e.message === "bad csrf") {
      // The link was opened again in another tab, which replaced this tab's token: take the
      // current one, and let the visitor send again.
      void onRecheck();
      setError("This link was opened again in another tab. Send it again from here.");
      return;
    }
    setError(e instanceof PublicError && e.status > 0 ? e.message : fallback);
  };

  const saveName = async () => {
    const n = name.trim();
    if (!n || busy) return;
    setBusy("name");
    setError(null);
    try {
      const r = await publicApi.setName(csrf, n);
      setRenaming(false);
      onShare(r.share);
    } catch (e) {
      fail(e, "Couldn’t save your name — try again.");
    } finally {
      setBusy(null);
    }
  };

  if (share.name_required || !share.name || renaming) {
    return (
      <form
        className={styles.composer}
        data-testid="share-name-form"
        onSubmit={(e) => {
          e.preventDefault();
          void saveName();
        }}
      >
        <label className={styles.label} htmlFor="share-name">
          Your name — shown with your messages
        </label>
        <div className={styles.row}>
          <input
            id="share-name"
            className={styles.input}
            value={name}
            maxLength={40}
            autoComplete="nickname"
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" className={styles.primary} disabled={!name.trim() || busy === "name"}>
            Continue
          </button>
          {renaming && (
            <button type="button" className={styles.ghost} onClick={() => setRenaming(false)}>
              Cancel
            </button>
          )}
        </div>
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
        <p className={styles.hint}>
          You can watch without a name. To send messages, tell the owner who you are. Anyone can
          type any name, so it isn’t a verified identity.
        </p>
      </form>
    );
  }

  if (!snap.can_send) {
    return (
      <footer className={styles.viewOnly} data-testid="share-cannot-send">
        <b>CAN’T SEND RIGHT NOW</b> ·{" "}
        {snap.mission_held
          ? "A mission is driving this session, so it can be watched but not sent to."
          : "This session isn’t taking messages right now."}
      </footer>
    );
  }

  const upload = async (files: FileList | null) => {
    if (!files || busy) return;
    setError(null);
    setBusy("upload");
    try {
      for (const file of Array.from(files).slice(0, MAX_IMAGES - images.length)) {
        const r = await publicApi.upload(csrf, file);
        setImages((prev) => [
          ...prev,
          { stored: r.stored, preview: URL.createObjectURL(file), name: file.name },
        ]);
      }
    } catch (e) {
      fail(e, "Couldn’t attach that image.");
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const send = async () => {
    const words = text.trim();
    if (!words || busy) return;
    const submitted = text; // as typed: what the composer showed when Send was pressed
    const attachments = images.map((p) => p.stored);
    const identity = JSON.stringify([words, attachments]);
    if (!retry.current || retry.current.identity !== identity) {
      retry.current = { key: crypto.randomUUID(), identity };
    }
    setBusy("send");
    setError(null);
    try {
      await publicApi.turn(csrf, { client_key: retry.current.key, text: words, attachments });
    } catch (e) {
      // A refusal (4xx) was not taken: a fresh key next time. Anything else may have landed, so
      // the same words keep the same key and the server answers the recorded turn.
      if (e instanceof PublicError && e.status >= 400 && e.status < 500) retry.current = null;
      fail(e, "That message may not have gone through — send it again to retry safely.");
      setBusy(null);
      return;
    }
    // Sent. Only now is the draft cleared — and only what was sent: words typed while the send
    // was in flight stay (#1444 review). A failed re-read afterwards is not a failed send.
    retry.current = null;
    images.filter((p) => attachments.includes(p.stored)).forEach((p) => URL.revokeObjectURL(p.preview));
    setImages((prev) => prev.filter((p) => !attachments.includes(p.stored)));
    // Words typed after Send was pressed are what remains; the sent part is not sent twice.
    setText((cur) => (cur.startsWith(submitted) ? cur.slice(submitted.length).trimStart() : cur));
    setBusy(null);
    await onSent();
  };

  const stop = async () => {
    if (!snap.active_turn || busy) return;
    setBusy("stop");
    setError(null);
    try {
      await publicApi.interrupt(csrf, snap.active_turn);
    } catch (e) {
      fail(e, "Couldn’t stop the current turn.");
      setBusy(null);
      return;
    }
    setBusy(null);
    await onSent();
  };

  return (
    <form
      className={styles.composer}
      data-testid="share-composer"
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      {images.length > 0 && (
        <ul className={styles.pending}>
          {images.map((p) => (
            <li key={p.stored} className={styles.pendingItem}>
              <img src={p.preview} alt={p.name} className={styles.thumb} />
              <button
                type="button"
                className={styles.iconBtn}
                aria-label={`Remove ${p.name}`}
                onClick={() => {
                  URL.revokeObjectURL(p.preview);
                  setImages((prev) => prev.filter((x) => x.stored !== p.stored));
                }}
              >
                <X size={14} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className={styles.row}>
        {snap.images && (
          <>
            <button
              type="button"
              className={styles.iconBtn}
              aria-label="Attach images"
              disabled={busy !== null || images.length >= MAX_IMAGES}
              onClick={() => fileRef.current?.click()}
            >
              <Paperclip size={16} aria-hidden="true" />
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={IMAGE_TYPES}
              multiple
              hidden
              onChange={(e) => void upload(e.target.files)}
            />
          </>
        )}
        <textarea
          className={`${styles.input} ${styles.textarea}`}
          aria-label="Message the agent"
          placeholder="Message the agent…"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {snap.active_turn && (
          <button type="button" className={styles.ghost} disabled={busy !== null} onClick={() => void stop()}>
            Stop
          </button>
        )}
        <button type="submit" className={styles.primary} disabled={busy !== null || !text.trim()}>
          Send
        </button>
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <p className={styles.hint}>
        Sending as <b>{share.name}</b> ·{" "}
        <button type="button" className={styles.linkBtn} onClick={() => setRenaming(true)}>
          change name
        </button>
      </p>
    </form>
  );
}
