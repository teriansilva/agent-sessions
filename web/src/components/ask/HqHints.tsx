import { useEffect, useRef, useState } from "react";

import a from "./AskConsole.module.css";
import {
  browserStore,
  HINTS,
  KIND_LABELS,
  lastSet,
  loadShown,
  pickHints,
  recordShown,
  type Hint,
} from "./hqHints";

/** "TRY ASKING HQ" in the empty thread (#2265): three example questions, a new set each time the
 *  empty thread mounts (the parent keys it by thread, so New conversation remounts it). A pick
 *  only hands the question to `onPick`, which fills the composer: nothing is ever sent from here.
 *  While the composer holds a draft (text or a staged file) the hints are disabled and say so in
 *  plain view. */
export function HqHints({
  locked,
  onPick,
  now = Date.now,
}: {
  locked: boolean;
  onPick: (text: string) => void;
  /** Tests inject the clock. */
  now?: () => number;
}) {
  // Chosen once per mount (a re-render keeps the set); recorded by the effect below.
  const [set, setSet] = useState<Hint[]>(() => {
    const shown = loadShown(browserStore());
    return pickHints(HINTS, shown, lastSet(shown));
  });
  const clock = useRef(now);
  // Recorded when THIS set is put on screen: once per set, never on a re-render.
  useEffect(() => {
    recordShown(browserStore(), set, clock.current());
  }, [set]);

  const other = () => {
    const shown = loadShown(browserStore());
    setSet(pickHints(HINTS, shown, new Set(set.map((h) => h.id))));
  };

  if (!set.length) return null;
  return (
    <section
      className={a.hqHints}
      aria-labelledby="hq-hints-label"
      data-testid="hq-hints"
    >
      <div className={a.hqHintsHead}>
        {/* A label, not a heading: the panel's title is its one h2, and the hints are part of the
            empty thread, not a section of the page. It names the region for assistive tech. */}
        <p id="hq-hints-label" className={a.hqHintsLabel}>
          Try asking HQ
        </p>
        <button
          type="button"
          className={a.hqHintsMore}
          onClick={other}
          data-testid="hq-hints-more"
        >
          <span aria-hidden="true">↻ </span>Other ideas
        </button>
      </div>
      {locked ? (
        <p
          className={a.hqHintsLock}
          data-testid="hq-hints-locked"
          id="hq-hints-lock"
        >
          Clear your draft to use a hint.
        </p>
      ) : null}
      <ul className={a.hqHintList}>
        {set.map((h) => (
          <li key={h.id}>
            <button
              type="button"
              className={a.hqHint}
              disabled={locked}
              aria-describedby={locked ? "hq-hints-lock" : undefined}
              onClick={() => onPick(h.text)}
              data-testid="hq-hint"
              data-hint={h.id}
            >
              <span className={a.hqHintKind}>{KIND_LABELS[h.kind]}</span>
              <span className={a.hqHintText}>{h.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
