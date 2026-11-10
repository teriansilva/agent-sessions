/** The shelf's lightbox (#1740, board 3): one item in the viewer it needs, its provenance, and the
 *  ONLY place a verdict is set (design decision 5) — so the note always travels with it.
 *
 *  A modal (`useModalDrawer`): portalled outside `#root`, which goes inert; focus moves in on open,
 *  Tab stays inside, and Escape, the scrim and Close all return focus to the tile that opened it.
 *  On a phone it is a full-height sheet with the verdict and previous/next docked at the bottom. */
import {
  Check,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  SquarePen,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import type { ShelfItem, ShelfVerdict } from "../../../types/shelf";
import action from "../../ui/actionButton.module.css";
import { useModalDrawer } from "../useModalDrawer";
import {
  ShelfDownload,
  ShelfGoneStage,
  ShelfMediaStage,
  ShelfTextDocument,
} from "./ShelfMedia";
import { SourceTag, StateWord } from "./ShelfTile";
import {
  KIND_LABEL,
  extOf,
  fmtBytes,
  fmtClock,
  isChanged,
  isGone,
  isInlineMedia,
  isTextDocument,
  shortSha,
} from "./shelfLogic";
import styles from "./shelf.module.css";

/** The ≤800px sheet layout, as a fact the markup follows: the docked verdict and the head's
 *  previous / next are RENDERED for one layout only, never just hidden, so the focus trap never
 *  wraps onto a control that cannot take focus. */
const PHONE = "(max-width: 800px)";
const hasMedia = () => typeof window !== "undefined" && typeof window.matchMedia === "function";
function subscribePhone(cb: () => void) {
  if (!hasMedia()) return () => {};
  const mq = window.matchMedia(PHONE);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}
function usePhone(): boolean {
  return useSyncExternalStore(
    subscribePhone,
    () => hasMedia() && window.matchMedia(PHONE).matches,
    () => false,
  );
}

/** Only an http(s) URL becomes a link. The server refuses anything else at delivery; this is the
 *  second line, so a manifest that bypassed it still renders text, never a `javascript:` href. */
const safeHref = (url: string | undefined): string | null =>
  url && /^https?:\/\//i.test(url) ? url : null;

export type VerdictResult = { ok: true } | { ok: false; message: string };

export function ShelfLightbox({
  missionId,
  items,
  index,
  sessionLabel,
  onIndex,
  onClose,
  onVerdict,
  triggerRef,
}: {
  missionId: string;
  /** The items in the order the shelf shows them (filters applied): previous / next walk this. */
  items: ShelfItem[];
  index: number;
  sessionLabel: (key: string | null) => string;
  onIndex: (i: number) => void;
  onClose: () => void;
  /** Record a verdict and note for `base` (the item as this lightbox shows it). */
  onVerdict: (base: ShelfItem, verdict: ShelfVerdict, note: string) => Promise<VerdictResult>;
  triggerRef: RefObject<HTMLElement | null>;
}) {
  const item = items[index];
  const phone = usePhone();
  const panel = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const noteId = useId();
  useModalDrawer({
    active: true,
    panelRef: panel,
    initialFocusRef: closeBtn,
    triggerRef,
    onClose,
  });

  const [note, setNote] = useState(item?.note ?? "");
  /** What is being saved: a verdict (its buttons wait) or only a note (on blur; the buttons stay
   *  live). A verdict pressed while a note save is in flight is QUEUED and sent the moment that
   *  save settles, against the shelf it produced — never swallowed (Hermes 7899). */
  const [saving, setSaving] = useState<"verdict" | "note" | null>(null);
  const [queued, setQueued] = useState<{ id: string; verdict: ShelfVerdict } | null>(null);
  /** The item on screen now: an answer for another one never paints its status here. */
  const shownRef = useRef<string | undefined>(item?.id);
  const [status, setStatus] = useState<string | null>(null);
  const [fit, setFit] = useState(true);
  // A new item starts from its own stored note.
  const [shownId, setShownId] = useState(item?.id);
  if (item && item.id !== shownId) {
    setShownId(item.id);
    setNote(item.note);
    setStatus(null);
    setFit(true);
  }
  useEffect(() => {
    shownRef.current = item?.id;
  }, [item?.id]);

  const go = useCallback(
    (d: number) => {
      if (!items.length) return;
      onIndex((index + d + items.length) % items.length);
    },
    [index, items.length, onIndex],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable)) return;
      if (t?.closest("video, audio")) return; // the player's own arrow keys seek
      if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "ArrowRight") go(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [go]);

  const save = useCallback(
    async (base: ShelfItem, verdict: ShelfVerdict, text: string, what: "verdict" | "note") => {
      setSaving(what);
      setStatus(null);
      const r = await onVerdict(base, verdict, text);
      setSaving(null);
      if (!r.ok && shownRef.current === base.id) setStatus(r.message);
    },
    [onVerdict],
  );

  // A queued verdict goes once the save before it settled — after the re-render that folded its
  // answer in, so it is made against the item and revision that answer produced.
  useEffect(() => {
    if (saving !== null || !queued || !item) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the queue is drained exactly once
    setQueued(null);
    if (queued.id === item.id) void save(item, queued.verdict, note, "verdict");
  }, [saving, queued, item, note, save]);

  if (!item) return null;

  const pressVerdict = (v: ShelfVerdict) => {
    if (saving === "note") setQueued({ id: item.id, verdict: v });
    else void save(item, v, note, "verdict");
  };
  const noteDirty = note.trim() !== item.note;

  const radios = (
    <div className={styles.verdictRadios} role="radiogroup" aria-label="Verdict">
      {(
        [
          ["good", "Looks good", Check, styles.vGood],
          ["needs_work", "Needs work", SquarePen, styles.vWork],
        ] as const
      ).map(([v, word, Icon, cls]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={(queued?.id === item.id ? queued.verdict : item.verdict) === v}
          className={`${styles.vBtn} ${cls}`}
          disabled={saving === "verdict"}
          onClick={() => pressVerdict(v)}
          data-verdict-btn={v}
        >
          <Icon size={16} aria-hidden="true" />
          {word}
        </button>
      ))}
    </div>
  );
  // The phone dock: previous / next sit BESIDE the radio group, never inside it.
  const verdictButtons = (dock: boolean) =>
    dock ? (
      <div className={styles.verdict}>
        <button
          type="button"
          className={`${action.icon} ${action.ghost}`}
          aria-label="Previous item"
          onClick={() => go(-1)}
        >
          <ChevronLeft size={16} aria-hidden="true" />
        </button>
        {radios}
        <button
          type="button"
          className={`${action.icon} ${action.ghost}`}
          aria-label="Next item"
          onClick={() => go(1)}
        >
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      </div>
    ) : (
      radios
    );

  const ext = (extOf(item.name) || "").toUpperCase();
  const viewerTag = [
    "Viewer",
    KIND_LABEL[item.kind],
    [ext, item.size !== null ? fmtBytes(item.size) : ""].filter(Boolean).join(" · "),
  ]
    .filter(Boolean)
    .join(" // ");

  let viewer: ReactNode;
  let checker = false;
  if (item.ref_type === "path" && isGone(item)) {
    viewer = <ShelfGoneStage item={item} />;
  } else if (isInlineMedia(item)) {
    checker = item.kind === "image";
    viewer = <ShelfMediaStage key={item.id} missionId={missionId} item={item} />;
  } else if (isTextDocument(item)) {
    viewer = <ShelfTextDocument key={item.id} missionId={missionId} item={item} />;
  } else if (item.ref_type === "path") {
    viewer = (
      <div className={styles.stageMsg} data-shelf-download-only="">
        <span>
          {ext || "This file"} is download-only on the shelf: it is not a type the viewer shows in
          place.
        </span>
        <ShelfDownload missionId={missionId} item={item} />
      </div>
    );
  } else if (item.ref_type === "url" && safeHref(item.url)) {
    viewer = (
      <div className={styles.stageMsg}>
        <a
          className={styles.linkOut}
          href={safeHref(item.url)!}
          target="_blank"
          rel="noopener noreferrer"
          referrerPolicy="no-referrer"
          data-shelf-link=""
        >
          {item.host}
          {item.url_path}
          <ExternalLink size={14} aria-hidden="true" />
        </a>
        <span>Opens in a new tab. BattleLab never fetched it.</span>
      </div>
    );
  } else {
    viewer = (
      <pre className={styles.textDoc} data-shelf-text-item="" tabIndex={0}>
        {item.text}
      </pre>
    );
  }

  const rows: [string, ReactNode][] = [
    ["Source", `${item.source === "delivered" ? "Delivered" : "Found in git"} · ${fmtClock(item.created_at)}`],
    ["Session", sessionLabel(item.session_key)],
  ];
  if (item.ref_type === "path") {
    rows.push(["File", item.path ?? item.name ?? ""]);
    rows.push([
      "Check",
      item.file?.state === "changed" ? (
        <span className="nj-stamp is-warn" data-shelf-check="changed">
          Changed since delivered
        </span>
      ) : item.file?.state === "missing" ? (
        <StateWord word="Missing" led="idle" tone="idle" />
      ) : item.file?.state === "inaccessible" ? (
        <StateWord word="Not accessible" led="idle" tone="idle" />
      ) : (
        <StateWord word="Matches delivery" led="up" tone="ok" />
      ),
    ]);
    rows.push(["Delivered", `sha256 ${shortSha(item.sha256)} · ${fmtClock(item.created_at)}`]);
    if (isChanged(item)) {
      rows.push([
        "On disk",
        `sha256 ${item.file?.disk_sha256 ? shortSha(item.file.disk_sha256) : "not hashed (too large)"} · modified ${fmtClock(item.file?.disk_mtime)}`,
      ]);
    }
  } else if (item.ref_type === "url") {
    rows.push(["Link", item.url ?? ""]);
  }

  const savedLine = status ? (
    <div className={`${styles.saved} ${styles.isErr}`} role="alert" data-shelf-verdict-error="">
      {status}
    </div>
  ) : saving ? (
    <div className={styles.saved}>Saving…</div>
  ) : item.verdict ? (
    <div className={styles.saved} data-shelf-saved="">
      <span className="hud-led up" aria-hidden="true" />
      Saved {fmtClock(item.verdict_at)}
      {noteDirty ? " · note not saved yet" : ""}
    </div>
  ) : (
    <div className={styles.saved}>
      {noteDirty ? "Pick a verdict to save the note" : "Not reviewed yet"}
    </div>
  );

  return createPortal(
    <>
      <div className={styles.scrim} aria-hidden="true" />
      <div
        ref={panel}
        className={`${styles.lb} nj-frame`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-shelf-lightbox={item.id}
      >
        <div className={styles.lbHead}>
          <div className={styles.lbTitle}>
            <span className={`hud-tag ${styles.lbTag}`}>
              {KIND_LABEL[item.kind]} // <SourceTag item={item} />
            </span>
            <h2 id={titleId}>{item.title}</h2>
          </div>
          <span className={styles.pos}>
            {index + 1} / {items.length}
          </span>
          <div className={styles.nav}>
            {phone ? null : (
              <>
                <button
                  type="button"
                  className={`${action.icon} ${action.ghost}`}
                  aria-label="Previous item"
                  onClick={() => go(-1)}
                >
                  <ChevronLeft size={16} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className={`${action.icon} ${action.ghost}`}
                  aria-label="Next item"
                  onClick={() => go(1)}
                >
                  <ChevronRight size={16} aria-hidden="true" />
                </button>
              </>
            )}
            <button
              ref={closeBtn}
              type="button"
              className={`${action.icon} ${action.ghost}`}
              aria-label="Close"
              onClick={onClose}
              data-shelf-close=""
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className={styles.lbBody}>
          <div className={styles.stage}>
            <div className={styles.strip}>
              <span className="hud-tag">{viewerTag}</span>
              <div className={styles.stageBtns}>
                {checker ? (
                  <>
                    <button
                      type="button"
                      className={`${action.ghost} ${action.compact}`}
                      aria-pressed={fit}
                      onClick={() => setFit(true)}
                    >
                      Fit
                    </button>
                    <button
                      type="button"
                      className={`${action.ghost} ${action.compact}`}
                      aria-pressed={!fit}
                      onClick={() => setFit(false)}
                    >
                      1:1
                    </button>
                  </>
                ) : null}
                {item.ref_type === "path" && !isGone(item) && (isInlineMedia(item) || isTextDocument(item)) ? (
                  <ShelfDownload missionId={missionId} item={item} />
                ) : null}
              </div>
            </div>
            <div
              className={`${styles.canvas} ${checker ? styles.checker : ""} ${checker && !fit ? styles.actual : ""}`}
              data-shelf-stage={item.kind}
            >
              {viewer}
            </div>
          </div>
          <div className={styles.side}>
            <section>
              <p className={styles.lbl}>{item.why ? "Why · in the agent's words" : "Why"}</p>
              {item.why ? (
                <p className={styles.why}>{item.why}</p>
              ) : (
                <p className={styles.hint}>No reason was given with this item.</p>
              )}
            </section>
            <section>
              <dl className={styles.dl}>
                {rows.map(([k, v]) => (
                  <div key={k} className={styles.dlRow}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            </section>
            {item.ref_type === "url" && safeHref(item.url) ? (
              <section>
                <div className={styles.rowBtns}>
                  <a
                    className={`${action.ghost} ${action.compact}`}
                    href={safeHref(item.url)!}
                    target="_blank"
                    rel="noopener noreferrer"
                    referrerPolicy="no-referrer"
                  >
                    <ExternalLink size={14} aria-hidden="true" /> Open link
                  </a>
                </div>
              </section>
            ) : null}
            {phone ? null : (
              <section>
                <p className={styles.lbl}>Your verdict</p>
                {verdictButtons(false)}
              </section>
            )}
            <section>
              <label className="nj-label" htmlFor={noteId}>
                Note · optional
              </label>
              <textarea
                id={noteId}
                className="nj-field"
                value={note}
                maxLength={2000}
                placeholder="What should change? The note is kept with the verdict."
                onChange={(e) => setNote(e.target.value)}
                onBlur={() => {
                  if (item.verdict && noteDirty && !saving) void save(item, item.verdict, note, "note");
                }}
                data-shelf-note=""
              />
              {savedLine}
              <p className={styles.hint}>
                Verdicts are recorded on the mission; closing it stays yours.
              </p>
            </section>
          </div>
        </div>
        {phone ? <div className={styles.dock}>{verdictButtons(true)}</div> : null}
      </div>
    </>,
    document.body,
  );
}
