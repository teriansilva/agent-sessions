import { ArrowLeft, ArrowRight, Pause, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import dlg from "../components/HudDialog.module.css";
import { useFocusContainment } from "../components/pulse/useModalDrawer";
import { SlideCounter, SlideDots } from "../components/slideshow/Slideshow";
import { useSlideshow } from "../components/slideshow/useSlideshow";
import { useInertBehind } from "../components/templates/useInertBehind";
import type { WhatsNewRelease } from "./releases";
import styles from "./WhatsNewDialog.module.css";

const publicAsset = (path: string): string =>
  `${import.meta.env.BASE_URL}${path.replace(/^\/+/, "")}`;

/** What's new (#971): a short slideshow on the HUD dialog sheet.
 *
 *  It closes only by ✕, Escape, the last slide's button or a CTA — every one of which is a
 *  dismissal the shell records. A tap on the backdrop does nothing, so a stray tap cannot lose it.
 *  The illustrations play once and rest; Pause swaps each to its still variant. */
export function WhatsNewDialog({
  release,
  onDismiss,
  onNavigate,
}: {
  release: WhatsNewRelease;
  onDismiss: () => void;
  /** Go to an in-app route; the shell also records the dismissal. */
  onNavigate: (to: string) => void;
}) {
  const { slides } = release;
  const show = useSlideshow(slides.length);
  const slide = slides[show.index];
  const [paused, setPaused] = useState(false);
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [returnTo] = useState(() =>
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );

  useInertBehind(returnTo);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => {
    dialogRef.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onDismiss();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onDismiss]);

  const image = slide.image
    ? publicAsset(`${release.assetDir}/${slide.image}${paused ? "-still" : ""}.svg`)
    : null;
  const primary = slide.primary ?? (show.last ? "Let's go" : "Next");

  return createPortal(
    <div className={dlg.backdrop}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${styles.dialog}`}
        data-testid="whats-new"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            What&apos;s new // v{release.version}
          </span>
          <SlideCounter state={show} className={styles.count} />
          <button
            type="button"
            className={`${dlg.close} ${styles.close}`}
            aria-label="Close what's new"
            onClick={onDismiss}
          >
            <X size={16} />
          </button>
        </div>

        {image && <img key={image} className={styles.shot} src={image} alt="" />}

        {slide.tiles ? (
          <>
            <p className={styles.ver}>{slide.eyebrow}</p>
            <p className={styles.wordmark} aria-hidden="true">
              BATTLE<b>LAB</b>
            </p>
          </>
        ) : (
          <div className={styles.mediaRow}>
            <p className={styles.eyebrow}>{slide.eyebrow}</p>
            {image && (
              <button
                type="button"
                className={styles.pause}
                aria-pressed={paused}
                onClick={() => setPaused((p) => !p)}
              >
                <Pause size={12} aria-hidden="true" />
                Pause animation
              </button>
            )}
          </div>
        )}

        <div aria-live="polite" aria-atomic="true">
          <h2 className={styles.title}>{slide.title}</h2>
          {slide.body && <p className={styles.body}>{slide.body}</p>}
        </div>

        {slide.tiles && (
          <div className={styles.tiles}>
            {slide.tiles.map((t) => (
              <button
                key={t.slide}
                type="button"
                className={styles.tile}
                onClick={() => show.goTo(slides.findIndex((s) => s.id === t.slide))}
              >
                <b>{t.label}</b>
                <span>{t.text}</span>
              </button>
            ))}
          </div>
        )}

        {slide.points && (
          <ul className={styles.points}>
            {slide.points.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}

        {slide.items && (
          <dl className={styles.items}>
            {slide.items.map((it) => (
              <div key={it.title}>
                <dt>{it.title}</dt>
                <dd>{it.text}</dd>
              </div>
            ))}
          </dl>
        )}

        {slide.cta?.to && (
          <button
            type="button"
            className={styles.cta}
            onClick={() => onNavigate(slide.cta!.to!)}
          >
            {slide.cta.label} <ArrowRight size={13} aria-hidden="true" />
          </button>
        )}
        {slide.cta?.href && (
          <a
            className={styles.cta}
            href={slide.cta.href}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onDismiss}
          >
            {slide.cta.label} <ArrowRight size={13} aria-hidden="true" />
          </a>
        )}

        <div className={`${dlg.actions} ${styles.foot}`}>
          <SlideDots state={show} />
          <span className={styles.grow} />
          {!show.first && (
            <button type="button" className={dlg.cancel} onClick={show.back}>
              <ArrowLeft size={14} aria-hidden="true" /> Back
            </button>
          )}
          <button
            type="button"
            className={dlg.go}
            onClick={show.last ? onDismiss : show.next}
          >
            {primary}
            {!show.last && <ArrowRight size={14} aria-hidden="true" />}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
