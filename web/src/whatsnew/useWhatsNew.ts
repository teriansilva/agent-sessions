import { useState } from "react";
import { newestRelease, seenCovers, whatsNewDue, type DueInput } from "./due";
import type { WhatsNewRelease } from "./releases";

/** What's new in the shell (#971): when the dialog opens, how long it stays, and what closing it
 *  records.
 *
 *  **Eligibility decides when it OPENS, not how long it stays** (#977 review). `whatsNewDue` is
 *  re-read on every render, and its inputs move underneath an open dialog: the version poll runs
 *  every five minutes and on every return to the tab, and a config refresh can land a
 *  `whats_new_seen` that already covers the notes on screen. Rendering straight from it unmounted
 *  the dialog mid-read — no dismissal, no write, reading position gone. So the release that opened
 *  it is held until the operator closes it, and only the close paths let it go. */

export interface WhatsNewState {
  /** The notes on screen, or `null`. */
  release: WhatsNewRelease | null;
  /** Reopen the newest notes (the tour's footer, Settings → About). Shows even when not due. */
  open: () => void;
  /** Every close path: ✕, Escape, the last slide's button, a CTA. */
  dismiss: () => void;
  /** Finishing (or skipping) setup covers the current notes in this tab, saved or not. */
  markSetupDone: () => void;
}

export function useWhatsNew(
  input: Omit<DueInput, "dismissed"> & {
    /** Record `version` as seen on the server. Called only when that would raise what is stored. */
    persist: (version: string) => void;
  },
): WhatsNewState {
  const { persist, ...due } = input;
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());
  const [manual, setManual] = useState(false);
  const [held, setHeld] = useState<WhatsNewRelease | null>(null);

  const auto = whatsNewDue({ ...due, dismissed });
  // Adjusted during render, not in an effect: the dialog must not flash closed for a frame while an
  // effect catches up.
  if (auto && !held) setHeld(auto);
  const release = manual ? newestRelease(due.releases) : (held ?? auto);

  const onboardedNow = due.config?.onboarded === true;
  const seenNow = due.config?.whats_new_seen ?? null;
  // Plain functions: the React Compiler memoizes them, and hand-written dependency lists here were
  // what it refused to preserve.
  const dismiss = () => {
    if (!release) return;
    const shown = release.version;
    setManual(false);
    setHeld(null);
    // This tab stays dismissed whatever the write does; it is durable only if the server stores it.
    setDismissed((s) => new Set(s).add(shown));
    if (!onboardedNow || seenCovers(seenNow, shown)) return;
    persist(shown);
  };

  const open = () => setManual(true);

  const markSetupDone = () => {
    const newest = newestRelease(due.releases);
    if (newest) setDismissed((s) => new Set(s).add(newest.version));
  };

  return { release, open, dismiss, markSetupDone };
}
