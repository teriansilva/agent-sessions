/** The dashboard's primary HQ contact (#1525). The contact `useHqContact()` resolves (the selected
 *  one, or Random's pick of the day) as one button: the looping portrait, the name, *Primary HQ
 *  contact* and *Open HQ →*. Activating it opens the HQ panel exactly as the corner icon does.
 *
 *  With Off, the same place holds a placeholder: a neutral silhouette (no face, no generated image),
 *  "No HQ contact", and two actions, *Open HQ →* and *Select contact* (Settings → Interface → the
 *  gallery). Two actions make that card a container, not a button.
 *
 *  Calling HQ (#1397): a Call control sits under the card (Set up calls until calls are set up).
 *
 *  Desktop places it in the headline row's last column, which is the *Plan quota* tile's column
 *  (`Dashboard.module.css .top` shares the KPI strip's columns); ≤800px it is a full-width row. */
import { Link } from "react-router-dom";

import { settingsPath } from "../../routes/settingsTabs";
import { useAskPanel } from "../ask/askPanel";
import { CallButton } from "../call/CallButton";
import { HqPortrait } from "./HqPortrait";
import { useHqContact } from "./useHqContact";
import s from "./PrimaryContact.module.css";

/** Where *Select contact* lands: the gallery on Settings → Interface. */
export const HQ_GALLERY_ANCHOR = "hq-contact";

export function PrimaryContact({ className = "" }: { className?: string }) {
  const { ready, contact } = useHqContact();
  const { openAsk } = useAskPanel();
  if (!ready) return null;
  if (!contact) {
    return (
      <div
        className={`${s.card} ${s.placeholder} ${className}`}
        data-testid="hq-primary-contact"
        data-contact="off"
      >
        <span className={`${s.portrait} ${s.silhouette}`} aria-hidden="true">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 58 78"
            width="58"
            height="78"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
            data-testid="hq-silhouette"
          >
            <circle cx="29" cy="30" r="11" />
            <path d="M8 78v-6c0-11 9-18 21-18s21 7 21 18v6" />
          </svg>
        </span>
        <span className={s.text}>
          <span className={s.name}>No HQ contact</span>
          <span className={s.muted}>Choose who briefs you</span>
          <span className={s.acts}>
            <button
              type="button"
              className={s.textBtn}
              onClick={openAsk}
              data-testid="hq-primary-open"
            >
              Open HQ →
            </button>
            <Link
              className={s.ghost}
              to={settingsPath("appearance", HQ_GALLERY_ANCHOR)}
              data-testid="hq-select-contact"
            >
              Select contact
            </Link>
          </span>
        </span>
      </div>
    );
  }
  return (
    // Calling HQ (#1397): Call sits under the card, its own control — a button cannot hold one.
    <div className={`${s.withCall} ${className}`}>
    <button
      type="button"
      className={s.card}
      aria-label={`Open HQ — ${contact.name}`}
      onClick={openAsk}
      data-testid="hq-primary-contact"
      data-contact={contact.id}
    >
      <HqPortrait
        contact={contact}
        playing
        className={s.portrait}
        testId="hq-primary-portrait"
      />
      <span className={s.text}>
        <span className={s.name}>{contact.name}</span>
        <span className={s.label}>Primary HQ contact</span>
        <span className={s.open} data-testid="hq-primary-open">
          Open HQ →
        </span>
      </span>
    </button>
    <CallButton variant="card" testId="hq-primary-call" />
    </div>
  );
}
