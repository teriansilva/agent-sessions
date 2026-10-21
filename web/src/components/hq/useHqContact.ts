import { useEffect, useState } from "react";

import { useConfig } from "../../app/config";
import { noteHqConfig, useHqChoiceState } from "./hqChoiceStore";
import {
  coerceHqChoice,
  msToLocalMidnight,
  resolveHqContact,
  type HqChoice,
  type HqContact,
} from "./contacts";

/** Today's date, which moves at local midnight: a timer to the next one, re-checked whenever the tab
 *  comes back (a sleeping laptop's timer fires late, or never). */
export function useLocalDay(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    let timer = 0;
    const arm = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(tick, msToLocalMidnight(new Date()));
    };
    const tick = () => {
      setNow((prev) => (sameDay(prev, new Date()) ? prev : new Date()));
      arm();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    arm();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return now;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

export interface HqContactState {
  /** False until /api/config has loaded: nothing is shown yet, so Off never flashes a face. */
  ready: boolean;
  /** The stored choice, coerced: an id, "random" or "off". */
  choice: HqChoice;
  /** Who speaks today: null for Off. */
  contact: HqContact | null;
  /** What a turn request carries (`contact`): the contact's id, or "off". */
  wire: string;
}

/** The ONE resolver for the HQ header, the dashboard card and a turn's `contact`, so all three
 *  always name the same General (#1525). */
export function useHqContact(): HqContactState {
  const cfg = useConfig();
  // A choice the server accepted that no config read has confirmed yet wins (`hqChoiceStore`).
  const { persisted } = useHqChoiceState();
  // Every config this app applies reconciles the store: the server's newer word retires an
  // unconfirmed local choice.
  useEffect(() => noteHqConfig(cfg), [cfg]);
  const choice = coerceHqChoice(persisted ?? cfg?.hq_contact);
  const day = useLocalDay();
  const contact = cfg ? resolveHqContact(choice, day) : null;
  return { ready: cfg !== null, choice, contact, wire: contact ? contact.id : "off" };
}
