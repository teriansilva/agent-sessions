/** How the shelf shows a file item's bytes (#1740) — always through the mission-bound read path.
 *
 *  Typed viewers only: an image is an `<img>`, video and audio the browser's own player, a text
 *  document plain text in a `<pre>` (never HTML), everything else a Download. The server decides
 *  what may be inline (the bytes are sniffed against their extension on every read); this side
 *  never picks a type. Two transports, as the Files viewer (#1530): on a direct connection the
 *  element's `src` is the shelf's raw URL; through the Home Free tunnel the bytes come through the
 *  API seam into an object URL, capped at `FILE_BLOB_CAP`, and no `/api` URL is handed to the
 *  browser at all. Thumbnails take turns (`thumbSlot`, two at a time): the server streams at most
 *  four raw responses at once, and a grid of tiles must not take every one of them. */
import { Download, FileX } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import {
  api,
  ApiError,
  FILE_BLOB_CAP,
  isApiTunneled,
  missionShelfRawUrl,
} from "../../../lib/api";
import type { ShelfItem } from "../../../types/shelf";
import {
  BUSY_TRIES,
  REACHABLE_RETRIES,
  busyDelayMs,
  phaseForProbe,
  type Phase,
} from "../../files/mediaPreviewLogic";
import action from "../../ui/actionButton.module.css";
import { fmtBytes } from "./shelfLogic";
import styles from "./shelf.module.css";

// ---------------------------------------------------------------- the thumbnail queue

const THUMB_CONCURRENCY = 2;
let thumbActive = 0;
const thumbWaiting: (() => void)[] = [];

/** How long one thumbnail may hold its turn. A hung raw stream gives the turn back at this
 *  deadline instead of stalling every other tile for good (Hermes 7899). */
const THUMB_TURN_MS = 20_000;

/** Take a thumbnail turn; the returned function gives it back (exactly once, or at the deadline). */
function thumbSlot(): Promise<() => void> {
  return new Promise((resolve) => {
    const grant = () => {
      thumbActive++;
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        window.clearTimeout(deadline);
        thumbActive--;
        thumbWaiting.shift()?.();
      };
      const deadline = window.setTimeout(release, THUMB_TURN_MS);
      resolve(release);
    };
    if (thumbActive < THUMB_CONCURRENCY) grant();
    else thumbWaiting.push(grant);
  });
}

/** The largest image a tile brings in as its thumbnail. Bigger ones show their kind card. */
const THUMB_MAX = 8 * 1024 * 1024;

/** A tile's picture: the real image, fetched in its turn through the API seam. */
export function ShelfThumb({ missionId, item }: { missionId: string; item: ShelfItem }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if ((item.size ?? 0) > THUMB_MAX) return;
    const ctl = new AbortController();
    let objectUrl: string | null = null;
    let release: (() => void) | null = null;
    void (async () => {
      release = await thumbSlot();
      const stop = window.setTimeout(() => ctl.abort(), THUMB_TURN_MS);
      ctl.signal.addEventListener("abort", () => window.clearTimeout(stop));
      if (ctl.signal.aborted) {
        release();
        return;
      }
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const blob = await api.missionShelfBlob(missionId, item.id, { signal: ctl.signal });
          objectUrl = URL.createObjectURL(blob);
          setUrl(objectUrl);
          break;
        } catch (e) {
          if (ctl.signal.aborted) break;
          if (e instanceof ApiError && e.status === 503 && attempt < 2) {
            await new Promise((r) => setTimeout(r, 600 * (attempt + 1)));
            continue;
          }
          setFailed(true);
          break;
        }
      }
      release();
    })();
    return () => {
      ctl.abort();
      release?.();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // The bytes on disk are what changed when the hash did: re-read then.
  }, [missionId, item.id, item.size, item.file?.disk_sha256]);
  if (!url || failed) return null;
  return <img className={styles.thumb} src={url} alt="" draggable={false} data-shelf-thumb="" />;
}

/** A video's poster frame and duration (or an audio item's duration), read in a thumbnail turn
 *  from the element's own metadata — direct connections only; through the tunnel the whole file
 *  would have to come first, so the tile shows its kind card instead. */
export function ShelfDuration({
  missionId,
  item,
  onDuration,
}: {
  missionId: string;
  item: ShelfItem;
  onDuration: (s: number) => void;
}) {
  const [turn, setTurn] = useState(false);
  const release = useRef<(() => void) | null>(null);
  const [tunneled] = useState(isApiTunneled);
  useEffect(() => {
    if (tunneled) return;
    let alive = true;
    void thumbSlot().then((r) => {
      if (!alive) {
        r();
        return;
      }
      release.current = r;
      setTurn(true);
    });
    return () => {
      alive = false;
      release.current?.();
    };
  }, [tunneled]);
  if (tunneled || !turn) return null;
  const done = () => release.current?.();
  const src = `${missionShelfRawUrl(missionId, item.id)}#t=0.1`;
  if (item.kind === "video") {
    return (
      <video
        className={styles.thumb}
        src={src}
        preload="metadata"
        muted
        playsInline
        tabIndex={-1}
        aria-hidden="true"
        onLoadedData={done}
        onLoadedMetadata={(e) => onDuration(e.currentTarget.duration)}
        onError={done}
        data-shelf-poster=""
      />
    );
  }
  return (
    <audio
      src={src}
      preload="metadata"
      onLoadedMetadata={(e) => {
        onDuration(e.currentTarget.duration);
        done();
      }}
      onError={done}
      hidden
    />
  );
}

// ---------------------------------------------------------------- the lightbox stage

/** The browser's own viewer for an inline media item, with #1530's busy / can't-play states. */
export function ShelfMediaStage({
  missionId,
  item,
}: {
  missionId: string;
  item: ShelfItem;
}) {
  const [tunneled] = useState(isApiTunneled);
  const size = item.size ?? 0;
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>(() =>
    tunneled ? (size > FILE_BLOB_CAP ? { k: "toolarge" } : { k: "loading" }) : { k: "ready" },
  );
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const tries = useRef(0);
  const reachable = useRef(0);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  useEffect(() => {
    if (!tunneled || size > FILE_BLOB_CAP) return;
    const ctl = new AbortController();
    let url: string | null = null;
    api
      .missionShelfBlob(missionId, item.id, { signal: ctl.signal })
      .then((blob) => {
        url = URL.createObjectURL(blob);
        setBlobUrl(url);
        setPhase({ k: "ready" });
      })
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        if (e instanceof ApiError && e.status === 503) {
          const ra = (e.record as { retryAfter?: number | null } | undefined)?.retryAfter ?? null;
          busy({ status: 503, retryAfter: ra, detail: e.message });
        } else if (e instanceof ApiError && e.status === 413) setPhase({ k: "toolarge" });
        else if (e instanceof ApiError && e.status < 500)
          setPhase({ k: "unavailable", message: e.message });
        else setPhase({ k: "timedout" });
      });
    return () => {
      ctl.abort();
      if (url) URL.revokeObjectURL(url);
    };
    // `busy` reads refs and setters only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tunneled, missionId, item.id, size, attempt]);

  function retry() {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setPhase(tunneled ? { k: "loading" } : { k: "ready" });
    setAttempt((a) => a + 1);
  }

  function busy(probe: { status: number; retryAfter: number | null; detail: string }) {
    const n = tries.current;
    setPhase(phaseForProbe(probe, n));
    if (n >= BUSY_TRIES) return;
    tries.current = n + 1;
    timer.current = window.setTimeout(retry, busyDelayMs(probe.retryAfter, n));
  }

  async function onError() {
    if (tunneled) {
      setPhase({ k: "cantplay" });
      return;
    }
    try {
      const probe = await api.missionShelfProbe(missionId, item.id);
      if (probe.status === 503) busy(probe);
      else if ((probe.status === 200 || probe.status === 206) && reachable.current < REACHABLE_RETRIES) {
        reachable.current += 1;
        retry();
      } else setPhase(phaseForProbe(probe, tries.current));
    } catch {
      setPhase({ k: "unavailable", message: "The file could not be reached." });
    }
  }

  if (phase.k !== "ready") {
    return (
      <div className={styles.stageMsg} data-shelf-phase={phase.k}>
        <span>{phaseText(phase)}</span>
        {phase.k === "busy" || phase.k === "timedout" || phase.k === "cantplay" ? (
          <button type="button" className={`${action.ghost} ${action.compact}`} onClick={retry}>
            Retry now
          </button>
        ) : null}
      </div>
    );
  }
  const src = tunneled ? blobUrl ?? undefined : missionShelfRawUrl(missionId, item.id, false, attempt);
  if (!src) return null;
  if (item.kind === "image") {
    return (
      <img
        key={src}
        src={src}
        alt={item.title}
        onError={() => void onError()}
        data-media-el="image"
      />
    );
  }
  if (item.kind === "video") {
    return (
      <video
        key={src}
        src={src}
        controls
        preload="metadata"
        playsInline
        onError={() => void onError()}
        data-media-el="video"
      />
    );
  }
  return (
    <audio
      key={src}
      src={src}
      controls
      preload="metadata"
      onError={() => void onError()}
      data-media-el="audio"
    />
  );
}

function phaseText(p: Phase): string {
  switch (p.k) {
    case "loading":
      return "Loading…";
    case "busy":
      return p.waitS
        ? `The viewer is busy streaming other files. Trying again in ${p.waitS} s.`
        : "The viewer is busy streaming other files.";
    case "cantplay":
      return "This browser can't play it here. Download it instead.";
    case "unavailable":
      return p.message;
    case "toolarge":
      return `Over Home Free, files come through the tunnel up to ${fmtBytes(FILE_BLOB_CAP)}. Open BattleLab on the box's own address to view this one.`;
    case "timedout":
      return "The file did not arrive.";
    default:
      return "";
  }
}

/** A text document, as text. Fetched through the API seam (both transports) and put in a `<pre>`
 *  as a string: nothing in it is ever parsed as markup. */
export function ShelfTextDocument({ missionId, item }: { missionId: string; item: ShelfItem }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    api
      .missionShelfBlob(missionId, item.id, { signal: ctl.signal })
      .then((b) => b.text())
      .then((t) => setText(t))
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        setErr(e instanceof ApiError ? e.message : "The document could not be read.");
      });
    return () => ctl.abort();
  }, [missionId, item.id, item.file?.disk_sha256]);
  if (err) return <div className={styles.stageMsg}>{err}</div>;
  if (text === null) return <div className={styles.stageMsg}>Loading…</div>;
  return (
    <pre className={styles.textDoc} data-shelf-text-doc="" tabIndex={0}>
      {text}
    </pre>
  );
}

/** Download: a plain link on a direct connection (any size); through the tunnel a blob, up to
 *  `FILE_BLOB_CAP`. Always `download=1` — an attachment, `application/octet-stream`. */
export function ShelfDownload({
  missionId,
  item,
  children,
}: {
  missionId: string;
  item: ShelfItem;
  children?: ReactNode;
}) {
  const [tunneled] = useState(isApiTunneled);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const name = item.name ?? "download";
  const label = children ?? (
    <>
      <Download size={14} aria-hidden="true" /> Download
    </>
  );
  const cls = `${action.ghost} ${action.compact}`;
  if (!tunneled) {
    return (
      <a
        className={cls}
        href={missionShelfRawUrl(missionId, item.id, true)}
        download={name}
        data-shelf-download=""
      >
        {label}
      </a>
    );
  }
  const over = (item.size ?? 0) > FILE_BLOB_CAP;
  const run = async () => {
    setBusy(true);
    setFailed(null);
    try {
      const blob = await api.missionShelfBlob(missionId, item.id, { download: true });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (e) {
      setFailed(e instanceof ApiError ? e.message : "The download did not finish through Home Free.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className={cls}
        disabled={over || busy}
        onClick={() => void run()}
        data-shelf-download=""
      >
        {over ? `Download (over ${fmtBytes(FILE_BLOB_CAP)})` : busy ? "Downloading…" : label}
      </button>
      {failed ? <span className={styles.hint}>{failed}</span> : null}
    </>
  );
}

/** What the stage says for a file it cannot show (gone, or now outside the boundary). */
export function ShelfGoneStage({ item }: { item: ShelfItem }) {
  const missing = item.file?.state === "missing";
  return (
    <div className={styles.stageMsg} data-shelf-gone={item.file?.state}>
      <FileX aria-hidden="true" />
      <span>
        {missing
          ? "The file is no longer on disk. The item stays on the shelf with what was recorded when it was delivered."
          : "The file is now outside this mission's project folders (or under an excluded folder), so it can't be shown."}
      </span>
    </div>
  );
}
