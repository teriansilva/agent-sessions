/** An HQ contact's portrait (#1525): a premade, silent, seamless loop, decorative (`aria-hidden`):
 *  the name beside it is the label.
 *
 *  - It plays only while `playing` (the panel is open, the card is on screen) and the tab is
 *    visible; otherwise it is paused on its current frame.
 *  - In the Reduced and Off motion modes (#1531) only the poster renders: no `<video>` at all.
 *    While ambient motion is paused (hidden tab, 10 idle minutes) the loop pauses on its frame.
 *  - A loop that fails to load (both sources) falls back to the poster, so a missing file never
 *    leaves an empty frame.
 *
 *  `data-state` says which of these is on screen, for tests: `playing`, `paused`, `still` or
 *  `failed`. */
import { useEffect, useRef, useState } from "react";

import { useMotion } from "../../theme/motionStore";
import type { HqContact } from "./contacts";
import s from "./hq.module.css";

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

export function HqPortrait({
  contact,
  playing,
  className = "",
  testId = "hq-portrait",
}: {
  contact: HqContact;
  playing: boolean;
  className?: string;
  testId?: string;
}) {
  const { mode, paused } = useMotion();
  const reduce = mode !== "full";
  const visible = useDocumentVisible();
  const [failedId, setFailedId] = useState<string | null>(null);
  const failed = failedId === contact.id;
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const lastSourceRef = useRef<HTMLSourceElement | null>(null);
  const run = playing && visible && paused === null;

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (run) {
      // React sets no `muted` ATTRIBUTE (only the property, and late): say it before play().
      v.muted = true;
      // A refused autoplay (a browser policy) leaves the poster frame; never an error.
      void v.play()?.catch?.(() => undefined);
    } else {
      v.pause();
    }
  }, [run, contact.id, reduce, failed]);

  // A <source> that fails fires `error` on itself, not on the <video>, and does not bubble: the
  // LAST source failing means none could play.
  useEffect(() => {
    const src = lastSourceRef.current;
    if (!src) return;
    const id = contact.id;
    const onError = () => setFailedId(id);
    src.addEventListener("error", onError);
    return () => src.removeEventListener("error", onError);
  }, [contact.id, reduce, failed]);

  const frame = `${s.portrait} ${className}`;
  if (reduce || failed) {
    return (
      <img
        className={frame}
        src={contact.poster}
        alt=""
        aria-hidden="true"
        draggable={false}
        data-testid={testId}
        data-state={reduce ? "still" : "failed"}
        data-contact={contact.id}
      />
    );
  }
  return (
    <video
      key={contact.id}
      ref={videoRef}
      className={frame}
      poster={contact.poster}
      muted
      loop
      playsInline
      autoPlay={run}
      preload={run ? "auto" : "none"}
      aria-hidden="true"
      tabIndex={-1}
      disablePictureInPicture
      data-testid={testId}
      data-state={run ? "playing" : "paused"}
      data-contact={contact.id}
    >
      <source src={contact.webm} type="video/webm" />
      <source ref={lastSourceRef} src={contact.mp4} type="video/mp4" />
    </video>
  );
}
