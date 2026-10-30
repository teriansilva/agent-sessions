/** A shared file rendered in the conversation (#1707) — one card per kind.
 *
 *  The kind is the SERVER's (decided from the bytes): `image` renders inline, `text` is a View +
 *  Download card with a bounded, scrollable excerpt, `other` is Download only. This component
 *  never renders markup from a file — text goes into text nodes, and a non-image is never an
 *  `<img>`. Bytes are fetched through the app's `api` seam into object URLs (the Home Free tunnel
 *  rule, `lib/uploadObjectUrl.ts`), never a native `/api/…` src.
 *
 *  The fallback is one contract: a decode failure with the bytes still present keeps **Download**;
 *  a verified 404 (gone) shows the name and *gone*, with **no** Download. The distinction is the
 *  response status, never guesswork.
 */
import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import type { AskAttachment } from "../../types/api";
import a from "./AskConsole.module.css";

/** The blob's state: `src` for an image, `text` for an excerpt, `gone` on a verified 404,
 *  `failed` when the bytes are there but the preview cannot render, `ok` for a kind with no
 *  preview (`other`) that is present.
 *
 *  Absence is detected for EVERY kind, and without a large download: the tiny `?meta=1` probe
 *  answers the descriptor or 404 first (so a deleted PDF reaches *gone* and its Download
 *  disappears), and only image/text then fetch the bytes. */
type AttachmentState =
  | { state: "loading" }
  | { state: "ok" }
  | { state: "src"; src: string }
  | { state: "text"; text: string; truncated: boolean }
  | { state: "gone" }
  | { state: "failed" };

function useAttachment(
  id: string | null,
  aid: string | null,
  kind: string,
  enabled: boolean,
): AttachmentState {
  const [st, setSt] = useState<AttachmentState>({ state: "loading" });
  useEffect(() => {
    if (!id || !aid) return;
    let alive = true;
    let url: string | null = null;
    const ctl = new AbortController();
    (async () => {
      const meta = await api.askAttachmentMeta(id, aid, ctl.signal).catch((e: unknown) => {
        const status = (e as { status?: number })?.status;
        return status === 404 ? null : "failed";
      });
      if (!alive) return;
      if (meta === null) {
        setSt({ state: "gone" });
        return;
      }
      if (meta === "failed") {
        setSt({ state: "failed" });
        return;
      }
      if (kind === "other" || !enabled) {
        // A download-only kind is PRESENT; there is nothing to preview and nothing to fetch.
        setSt({ state: "ok" });
        return;
      }
      const blob = await api.askAttachmentBlob(id, aid, ctl.signal).catch(() => null);
      if (!alive) return;
      if (!blob) {
        setSt({ state: "failed" });
        return;
      }
      if (kind === "image") {
        url = URL.createObjectURL(blob);
        setSt({ state: "src", src: url });
        return;
      }
      // The FULL text: the card scrolls to the end (#1720 review — a 4 000-char cut labelled
      // "scroll for the rest" was a silent cut, which #1707 forbids).
      setSt({ state: "text", text: await blob.text(), truncated: false });
    })();
    return () => {
      alive = false;
      ctl.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [id, aid, kind, enabled]);
  return st;
}

/** Download through the app's fetch seam — never a native `/api/…` href (in Home Free app mode
 *  that href would ask the relay for the blob and leak its identifiers into the relay's log;
 *  `lib/uploadObjectUrl.ts`'s rule). Bytes → object URL → the browser's download, revoked after. */
function useDownload(id: string | null, aid: string | null, name: string) {
  return async () => {
    if (!id || !aid) return;
    const blob = await api.askAttachmentBlob(id, aid).catch(() => null);
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // The revocation is delayed: Safari needs the URL alive through the click.
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  };
}

/** One attachment's card. `cid` is the conversation it belongs to (bytes are keyed by it). */
export function AttachmentCard({
  cid,
  att,
  expanded = false,
}: {
  cid: string | null;
  att: AskAttachment;
  /** A text card starts expanded where the operator just sent it; stored turns start collapsed. */
  expanded?: boolean;
}) {
  const [open, setOpen] = useState(expanded);
  const [broken, setBroken] = useState(false);
  // A file shared as a SECRET (#1786) is never fetched back: the absence probe only.
  const secret = att.secret === true;
  // The absence probe runs for every kind; preview bytes only for an image, or a text
  // excerpt the operator opened.
  const preview = useAttachment(
    cid,
    att.id,
    secret ? "other" : att.kind,
    !secret && (att.kind === "image" || open),
  );
  const download = useDownload(cid, att.id, att.name);
  const gone = preview.state === "gone";

  return (
    <div
      className={a.card}
      data-testid="ask-attachment"
      data-kind={att.kind}
      data-secret={secret ? "true" : undefined}
    >
      <div className={a.cardHead}>
        <span className={a.cardName} title={att.name}>
          {att.name}
        </span>
        <span className={a.cardMeta}>
          {gone
            ? secret
              ? "moved or gone"
              : "gone"
            : `${secret ? "Secret" : att.kind === "image" ? "Image" : att.kind === "text" ? "Text" : "File"} · ${formatSize(att.size)}`}
        </span>
        <div className={a.cardActions}>
          {att.kind === "text" && !gone && !secret ? (
            <button
              type="button"
              className={`${a.cardBtn}`}
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
              data-testid="ask-attachment-view"
            >
              {open ? "Hide" : "View"}
            </button>
          ) : null}
          {!gone && !secret ? (
            <button
              type="button"
              className={a.cardBtn}
              onClick={() => void download()}
              data-testid="ask-attachment-download"
            >
              Download
            </button>
          ) : null}
        </div>
      </div>
      {secret && !gone ? (
        <div className={a.cardNote} data-testid="ask-attachment-secret">
          Shared as a secret: HQ sees only its name and size, and it is never shown here again.
        </div>
      ) : null}
      {att.kind === "image" && !gone && !secret ? (
        preview.state === "src" && !broken ? (
          <img
            className={a.cardImg}
            src={preview.src}
            alt={att.name}
            onError={() => setBroken(true)}
            data-testid="ask-attachment-img"
          />
        ) : preview.state === "failed" || broken ? (
          // Bytes present, decode failed: the Download above stays.
          <div className={a.cardFallback} data-testid="ask-attachment-fallback">
            Preview unavailable
          </div>
        ) : (
          <div className={a.cardFallback}>Loading…</div>
        )
      ) : null}
      {att.kind === "text" && open && !gone && !secret ? (
        preview.state === "text" ? (
          <>
            <pre className={a.cardPreview} data-testid="ask-attachment-preview">
              {preview.text}
            </pre>
            <div className={a.cardNote}>Scrollable — the whole file is here</div>
          </>
        ) : (
          <div className={a.cardFallback}>
            {preview.state === "failed" ? "Preview unavailable" : "Loading…"}
          </div>
        )
      ) : null}
    </div>
  );
}

/** Every attachment of one turn, in the order they were shared. */
export function AttachmentList({
  cid,
  attachments,
  expanded = false,
}: {
  cid: string | null;
  attachments: AskAttachment[];
  expanded?: boolean;
}) {
  if (!attachments.length) return null;
  return (
    <div className={a.cards} data-testid="ask-attachments">
      {attachments.map((att) => (
        <AttachmentCard key={att.id} cid={cid} att={att} expanded={expanded} />
      ))}
    </div>
  );
}

function formatSize(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}
