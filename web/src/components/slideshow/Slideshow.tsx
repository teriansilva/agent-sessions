import styles from "./Slideshow.module.css";
import type { SlideshowState } from "./useSlideshow";

/** Position controls for a slideshow driven by `useSlideshow` (#971), shared by the setup tour and
 *  What's new. */

/** Clickable position dots. Hidden at ≤800px: a dot's 24px hit area is under the 44px touch floor,
 *  and there the counter carries the position instead. */
export function SlideDots({ state }: { state: SlideshowState }) {
  return (
    <div className={styles.dots} role="group" aria-label="Slides">
      {Array.from({ length: state.count }, (_, i) => (
        <button
          key={i}
          type="button"
          className={styles.dot}
          aria-label={`Slide ${i + 1} of ${state.count}`}
          aria-current={i === state.index ? "step" : undefined}
          onClick={() => state.goTo(i)}
        >
          <i aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}

export function SlideCounter({
  state,
  className,
}: {
  state: SlideshowState;
  className?: string;
}) {
  return (
    <span className={className ? `${styles.counter} ${className}` : styles.counter}>
      {state.index + 1} / {state.count}
    </span>
  );
}
