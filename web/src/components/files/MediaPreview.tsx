import { useEffect, useRef, useState, type ReactNode } from "react";
import { Download, RotateCw } from "lucide-react";
import { api, ApiError, FILE_BLOB_CAP, filesRawUrl, isApiTunneled } from "../../lib/api";
import {
  BUSY_TRIES,
  REACHABLE_RETRIES,
  busyDelayMs,
  fmtBytes,
  fmtDuration,
  phaseForProbe,
  subtype,
  type MediaKind,
  type Phase,
} from "./mediaPreviewLogic";
import styles from "./mediaPreview.module.css";

/** A Download for any binary: a plain link on a direct connection (any size); through the tunnel
 *  as a blob in app mode, up to `FILE_BLOB_CAP`. */
export function DownloadButton({
  path,
  name,
  size,
  tunneled = isApiTunneled(),
}: {
  path: string;
  name: string;
  size: number;
  tunneled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  if (!tunneled) {
    return (
      <a className={styles.btn} href={filesRawUrl(path, true)} download={name} data-media-download="">
        <Download size={14} aria-hidden="true" /> Download
      </a>
    );
  }
  const over = size > FILE_BLOB_CAP;
  const run = async () => {
    setBusy(true);
    setFailed(null);
    try {
      const blob = await api.fileBlob(path, { download: true });
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
        className={styles.btn}
        disabled={over || busy}
        aria-disabled={over || busy}
        title={
          over
            ? `Over Home Free, downloads come through the tunnel up to ${fmtBytes(FILE_BLOB_CAP)}. Open BattleLab on the box's own address to download this file.`
            : undefined
        }
        onClick={() => void run()}
        data-media-download=""
      >
        <Download size={14} aria-hidden="true" />{" "}
        {over ? `Download (over ${fmtBytes(FILE_BLOB_CAP)})` : busy ? "Downloading…" : "Download"}
      </button>
      {failed && <span className={styles.tag}>{failed}</span>}
    </>
  );
}

/** The Files viewer's media stage (#1530): the browser's own player for an allowlisted type.
 *
 *  Two transports, chosen once per mount. **Direct**: the element's `src` is the raw URL, so the
 *  browser streams with Range and seeks. **Home Free app mode**: the bytes come through the API
 *  seam into an object URL (aborted on unmount, revoked), capped at `FILE_BLOB_CAP` — a native
 *  `src` there would ask the relay instead of the box. Mount it with `key={path}` so a new file
 *  starts from a clean slate. */
export function MediaPreview({
  path,
  name,
  size,
  mime,
  kind,
}: {
  path: string;
  name: string;
  size: number;
  mime?: string;
  kind: MediaKind;
}) {
  const [tunneled] = useState(isApiTunneled);
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<Phase>(() =>
    tunneled ? (size > FILE_BLOB_CAP ? { k: "toolarge" } : { k: "loading" }) : { k: "ready" },
  );
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [fit, setFit] = useState(true);
  const [facts, setFacts] = useState<string>("");
  const timer = useRef<number | null>(null);
  const probeCtl = useRef<AbortController | null>(null);
  const triesRef = useRef(0);
  // A failed element whose probe then SUCCEEDS earns this many re-requests before the viewer calls
  // it undecodable: the slot it was refused may simply have freed in between (503 → 206, Hermes,
  // PR #1533). Bounded, so a genuinely broken file still ends on "Can't play here".
  const reachableRetriesRef = useRef(0);

  // Every pending retry and probe dies with the viewer (closed, or another file opened).
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      probeCtl.current?.abort();
    },
    [],
  );

  // App mode: bring the bytes through the tunnel. Re-runs on a retry (`attempt`).
  useEffect(() => {
    if (!tunneled || size > FILE_BLOB_CAP) return;
    const ctl = new AbortController();
    let url: string | null = null;
    api
      .fileBlob(path, { signal: ctl.signal })
      .then((blob) => {
        url = URL.createObjectURL(blob);
        setBlobUrl(url);
        setPhase({ k: "ready" });
      })
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return;
        if (e instanceof ApiError && e.status === 503) {
          const ra = (e.record as { retryAfter?: number | null } | undefined)?.retryAfter ?? null;
          scheduleBusy({ status: 503, retryAfter: ra, detail: e.message });
        } else if (e instanceof ApiError && e.status === 413) {
          setPhase({ k: "toolarge" });
        } else if (e instanceof ApiError && e.status < 500) {
          setPhase({ k: "unavailable", message: e.message });
        } else {
          setPhase({ k: "timedout" });
        }
      });
    return () => {
      ctl.abort();
      if (url) URL.revokeObjectURL(url);
    };
    // `scheduleBusy` only reads refs and setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tunneled, path, size, attempt]);

  function retryNow() {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setPhase(tunneled ? { k: "loading" } : { k: "ready" });
    setAttempt((a) => a + 1);
  }

  function scheduleBusy(probe: { status: number; retryAfter: number | null; detail: string }) {
    const tries = triesRef.current;
    setPhase(phaseForProbe(probe, tries));
    if (tries >= BUSY_TRIES) return; // out of automatic tries: Retry now is the way on
    triesRef.current = tries + 1;
    timer.current = window.setTimeout(retryNow, busyDelayMs(probe.retryAfter, tries));
  }

  async function onMediaError() {
    if (tunneled) {
      // The bytes already arrived through the tunnel, so this is the browser failing to decode.
      setPhase({ k: "cantplay" });
      return;
    }
    probeCtl.current?.abort();
    const ctl = new AbortController();
    probeCtl.current = ctl;
    try {
      const probe = await api.filesRawProbe(path, ctl.signal);
      if (ctl.signal.aborted) return;
      if (probe.status === 503) scheduleBusy(probe);
      else if (
        (probe.status === 200 || probe.status === 206) &&
        reachableRetriesRef.current < REACHABLE_RETRIES
      ) {
        reachableRetriesRef.current += 1;
        retryNow();
      } else setPhase(phaseForProbe(probe, triesRef.current));
    } catch {
      if (!ctl.signal.aborted)
        setPhase({ k: "unavailable", message: "The file could not be reached." });
    }
  }

  const src = tunneled ? blobUrl : filesRawUrl(path, false, attempt);
  const label = kind.toUpperCase();
  const sub = subtype(mime);
  const tagFacts = [sub, facts, fmtBytes(size)].filter(Boolean).join(" · ");
  const note = (tag: string, body: string, actions?: ReactNode, bad = false) => (
    <div className={`${styles.note} ${bad ? styles.noteBad : ""}`} role={bad ? "alert" : "status"}>
      <span className={styles.tag}>{tag}</span>
      <span>{body}</span>
      <div className={styles.row}>
        {actions}
        <DownloadButton path={path} name={name} size={size} tunneled={tunneled} />
      </div>
    </div>
  );

  let stage: ReactNode;
  if (phase.k === "loading") {
    stage = (
      <div className={styles.note} role="status">
        <span className={styles.tag}>Viewer // Home Free // Loading</span>
        <span>Bringing {name} through the tunnel…</span>
      </div>
    );
  } else if (phase.k === "busy") {
    const auto = phase.tries < BUSY_TRIES;
    stage = note(
      `Viewer // Busy`,
      auto
        ? `BattleLab is already streaming as many files as it allows. Trying again in ${Math.round(busyDelayMs(phase.waitS, phase.tries) / 1000)} s.`
        : "BattleLab is still streaming as many files as it allows. Try again when one finishes.",
      <button type="button" className={styles.btn} onClick={retryNow} data-media-retry="">
        <RotateCw size={14} aria-hidden="true" /> Retry now
      </button>,
      true,
    );
  } else if (phase.k === "cantplay") {
    stage = note(
      "Viewer // Can't play here",
      `${name} is ${mime ?? "this type"}, but this browser could not decode it. The file is fine; download it to open it elsewhere.`,
      null,
      true,
    );
  } else if (phase.k === "unavailable") {
    stage = note("Viewer // Unavailable", phase.message, null, true);
  } else if (phase.k === "toolarge") {
    stage = note(
      "Viewer // Home Free // Too large to preview",
      `${name} is ${fmtBytes(size)}. Over Home Free, previews and downloads come through the tunnel up to ${fmtBytes(FILE_BLOB_CAP)}. Open BattleLab on the box's own address to play or download it.`,
      null,
      true,
    );
  } else if (phase.k === "timedout") {
    stage = note(
      "Viewer // Home Free // Timed out",
      `${name} did not come through the tunnel in time. Try again, or open BattleLab on the box's own address to stream it.`,
      <button type="button" className={styles.btn} onClick={retryNow} data-media-retry="">
        <RotateCw size={14} aria-hidden="true" /> Retry
      </button>,
      true,
    );
  } else if (src && kind === "image") {
    stage = (
      <img
        key={src}
        className={styles.img}
        src={src}
        alt={name}
        data-media-el="image"
        onLoad={(e) =>
          setFacts(`${e.currentTarget.naturalWidth} × ${e.currentTarget.naturalHeight}`)
        }
        onError={() => void onMediaError()}
      />
    );
  } else if (src && kind === "video") {
    stage = (
      <video
        key={src}
        className={styles.video}
        src={src}
        controls
        playsInline
        preload="metadata"
        data-media-el="video"
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          setFacts(
            [v.videoWidth ? `${v.videoWidth} × ${v.videoHeight}` : "", fmtDuration(v.duration)]
              .filter(Boolean)
              .join(" · "),
          );
        }}
        onError={() => void onMediaError()}
      />
    );
  } else if (src && kind === "audio") {
    stage = (
      <div className={styles.audioCard}>
        <span className={styles.audioName}>{name}</span>
        <audio
          key={src}
          className={styles.audio}
          src={src}
          controls
          preload="metadata"
          data-media-el="audio"
          onLoadedMetadata={(e) => setFacts(fmtDuration(e.currentTarget.duration))}
          onError={() => void onMediaError()}
        />
      </div>
    );
  }

  const playing = phase.k === "ready";
  return (
    <div className={styles.media} data-media-preview={kind} data-media-phase={phase.k}>
      <div className={styles.strip}>
        <span className={styles.tag}>
          Viewer // <b>{label}</b> // {tagFacts}
        </span>
        <span className={styles.spacer} />
        {kind === "image" && playing && (
          <span className={styles.seg} role="group" aria-label="Zoom">
            <button
              type="button"
              className={styles.btn}
              aria-pressed={fit}
              onClick={() => setFit(true)}
              data-media-fit=""
            >
              Fit
            </button>
            <button
              type="button"
              className={styles.btn}
              aria-pressed={!fit}
              onClick={() => setFit(false)}
              data-media-actual=""
            >
              1:1
            </button>
          </span>
        )}
        {playing && <DownloadButton path={path} name={name} size={size} tunneled={tunneled} />}
      </div>
      <div
        className={`${styles.stage} ${kind === "image" && playing ? styles.checker : ""} ${kind === "image" && !fit ? styles.actual : ""}`}
      >
        {stage}
      </div>
    </div>
  );
}

/** A binary file the browser cannot show (#1530): say so, and offer the bytes. */
export function NotPreviewable({
  path,
  name,
  size,
  mime,
}: {
  path: string;
  name: string;
  size: number;
  mime?: string;
}) {
  return (
    <div className={styles.media} data-media-preview="none">
      <div className={styles.stage}>
        <div className={styles.note}>
          <span className={styles.tag}>Viewer // Not previewable</span>
          <span>
            {`${name} is ${mime ?? "binary"} (${fmtBytes(size)}) — not a type this browser can show. Download it to open it elsewhere.`}
          </span>
          <div className={styles.row}>
            <DownloadButton path={path} name={name} size={size} />
          </div>
        </div>
      </div>
    </div>
  );
}
