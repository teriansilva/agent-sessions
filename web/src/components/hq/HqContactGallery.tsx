/** Settings → Interface → "Select your primary HQ contact" (#1525): Random first (the default),
 *  then every contact in `contacts.ts`, then Off. A radio group of cards (arrow keys move and select,
 *  one Tab stop); the selected card plays its loop, the others show posters. Saved to the
 *  `hq_contact` pref, so the choice follows the operator to every device. */
import { EyeOff, Shuffle } from "lucide-react";
import { useEffect, useRef } from "react";
import { useLocation } from "react-router-dom";

import { useConfigReload } from "../../app/config";
import { onRadioGroupKeyDown, rovingTabIndex } from "../../lib/radioArrows";
import { CONTACTS, contactForDay, type HqChoice } from "./contacts";
import { HqPortrait } from "./HqPortrait";
import { pickHqChoice, retryHqChoiceRefresh, useHqChoiceState } from "./hqChoiceStore";
import { HQ_GALLERY_ANCHOR } from "./PrimaryContact";
import { useHqContact, useLocalDay } from "./useHqContact";
import s from "./HqContactGallery.module.css";

interface Card {
  key: HqChoice;
  name: string;
  region: string;
}

const CARDS: Card[] = [
  { key: "random", name: "Random", region: "A new contact every day" },
  ...CONTACTS.map((c) => ({ key: c.id, name: c.name, region: c.region })),
  { key: "off", name: "Off", region: "No portrait" },
];

export function HqContactGallery({
  subheadClass,
  hintClass,
}: {
  subheadClass: string;
  hintClass: string;
}) {
  const { choice: stored } = useHqContact();
  const reload = useConfigReload();
  const today = contactForDay(useLocalDay());
  // The pick in flight lives in the app-wide store (`hqChoiceStore`), so it outlives this gallery:
  // the latest pick shows at once, then what the server accepted, then the config (Hermes on #1535).
  const { pending, persisted, problem } = useHqChoiceState();
  const choice = pending ?? persisted ?? stored;

  // The dashboard's "Select contact" lands HERE (`#hq-contact`): an in-app link scrolls nowhere by
  // itself, so the gallery comes into view and its checked card takes focus.
  const { hash } = useLocation();
  const root = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (hash !== `#${HQ_GALLERY_ANCHOR}`) return;
    root.current?.scrollIntoView?.({ block: "start" });
    root.current?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]')?.focus();
  }, [hash]);

  const pick = (key: HqChoice) => {
    if (key !== choice) pickHqChoice(key, reload);
  };

  return (
    <div id={HQ_GALLERY_ANCHOR} ref={root} data-testid="hq-contact-setting">
      <h3 className={subheadClass} id="hq-contact-h">
        Select your primary HQ contact
      </h3>
      <p className={hintClass}>
        The General at the top of the HQ panel and on the dashboard. Your choice follows you to
        every device.
      </p>
      <div
        className={s.gallery}
        role="radiogroup"
        aria-labelledby="hq-contact-h"
        onKeyDown={onRadioGroupKeyDown}
        data-testid="hq-gallery"
      >
        {CARDS.map((c, i) => {
          const on = choice === c.key;
          const contact = CONTACTS.find((x) => x.id === c.key);
          return (
            <button
              key={c.key}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={rovingTabIndex(on, i, true)}
              className={s.card}
              onClick={() => pick(c.key)}
              data-testid="hq-card"
              data-key={c.key}
            >
              <span className={`${s.thumb} ${contact ? "" : s.glyph}`}>
                {contact ? (
                  on ? (
                    <HqPortrait contact={contact} playing className={s.thumbMedia} />
                  ) : (
                    <img className={s.thumbMedia} src={contact.poster} alt="" aria-hidden="true" />
                  )
                ) : c.key === "random" ? (
                  <>
                    <Shuffle size={26} strokeWidth={1.75} aria-hidden="true" />
                    <img
                      className={s.today}
                      src={today.poster}
                      alt=""
                      aria-hidden="true"
                      data-testid="hq-random-today"
                    />
                  </>
                ) : (
                  <EyeOff size={26} strokeWidth={1.75} aria-hidden="true" />
                )}
              </span>
              <span className={s.label}>
                <span className={s.name}>{c.name}</span>
                <span className={s.region}>{c.region}</span>
                {c.key === "random" ? (
                  <span className={s.region} data-testid="hq-random-pick">
                    Today: {today.name}
                  </span>
                ) : null}
                {on ? (
                  <span className={s.sel} data-testid="hq-selected">
                    ✓ Selected
                  </span>
                ) : null}
              </span>
            </button>
          );
        })}
      </div>
      {problem === "save" ? (
        <p className={hintClass} role="alert" data-testid="hq-contact-error">
          That choice could not be saved. Try again.
        </p>
      ) : problem === "refresh" ? (
        <p className={hintClass} role="alert" data-testid="hq-contact-refresh-error">
          Saved, but BattleLab could not reload its settings, so HQ may still show the previous
          contact.{" "}
          <button
            type="button"
            className={s.retry}
            onClick={() => retryHqChoiceRefresh(reload)}
            data-testid="hq-contact-refresh-retry"
          >
            Retry
          </button>
        </p>
      ) : null}
    </div>
  );
}
