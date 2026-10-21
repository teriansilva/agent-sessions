import { RELEASES, type WhatsNewRelease } from "./releases";
import { atLeast, compareVersions, parseVersion } from "./version";

/** When the What's new dialog auto-shows (#971). Pure, so every rule below is a table row in
 *  `due.test.ts` rather than a behaviour spread across the shell. */

export interface DueInput {
  config: {
    onboarded?: boolean;
    must_change_password?: boolean;
    whats_new_seen?: string | null;
  } | null;
  /** The setup wizard or the standalone tour is on screen. */
  wizardOpen: boolean;
  /** The loaded bundle's stamp (`whatsNewBundleVersion`), `"dev"` when unstamped. */
  bundle: string;
  /** The server's `/api/version`, `null` until an answer arrives. */
  server: string | null;
  /** `useAppVersion().updateReady` — a newer shell is waiting. */
  updateReady: boolean;
  /** Versions this tab already dismissed, saved or not. */
  dismissed: ReadonlySet<string>;
  releases?: readonly WhatsNewRelease[];
}

export function newestRelease(
  releases: readonly WhatsNewRelease[] = RELEASES,
): WhatsNewRelease | null {
  let best: WhatsNewRelease | null = null;
  for (const r of releases) {
    const v = parseVersion(r.version);
    if (!v) continue;
    const b = best && parseVersion(best.version);
    if (!b || compareVersions(v, b) > 0) best = r;
  }
  return best;
}

/** Whether a stored `whats_new_seen` already covers `version` — the numeric maximum, as the
 *  server stores it, so a write that would not raise it is skipped. */
export function seenCovers(seen: unknown, version: string): boolean {
  const s = parseVersion(seen);
  const v = parseVersion(version);
  return !!s && !!v && compareVersions(s, v) >= 0;
}

export function whatsNewDue(input: DueInput): WhatsNewRelease | null {
  const { config } = input;
  // A server that does not send the key predates #971; so does every e2e mock written before it.
  if (!config || !("whats_new_seen" in config)) return null;
  if (config.onboarded !== true || config.must_change_password) return null;
  if (input.wizardOpen) return null;
  // The release shell, proven by equality: a stamped bundle, a KNOWN server version, the same
  // version on both, and no fresh service-worker shell waiting. A stale tab holds old slides and
  // waits for the reload chip; an unstamped bundle cannot say which slides it carries.
  if (input.bundle === "dev" || input.server === null) return null;
  if (input.server !== input.bundle || input.updateReady) return null;
  const newest = newestRelease(input.releases);
  if (!newest || !atLeast(input.server, newest.version)) return null;
  if (seenCovers(config.whats_new_seen, newest.version)) return null;
  if (input.dismissed.has(newest.version)) return null;
  return newest;
}

/** `0.20.0` → `0.20`; a patch release keeps its patch. */
export function releaseLabel(version: string): string {
  const v = parseVersion(version);
  if (!v) return version;
  const [major, minor, patch] = v.release;
  return patch === 0 ? `${major}.${minor}` : `${major}.${minor}.${patch}`;
}

export function whatsNewLabel(
  releases: readonly WhatsNewRelease[] = RELEASES,
): string | null {
  const newest = newestRelease(releases);
  return newest ? `What's new in ${releaseLabel(newest.version)}` : null;
}

/** The stamp the gate compares against.
 *
 *  Shipped bundles are always stamped — the installer builds with `AGENT_SESSIONS_VERSION` — and a
 *  stamped bundle is returned as is. An unstamped build (vitest, the Playwright preview, pr-visual)
 *  reports `"dev"`, so nothing auto-shows there, unless an end-to-end test states the stamp it is
 *  standing in for on `window.__BATTLELAB_E2E_BUNDLE_VERSION__`. That override is read ONLY when
 *  the build is unstamped, so it has no effect on any installed bundle — and all it could ever do is
 *  decide whether a dialog of static text opens. */
export function whatsNewBundleVersion(stamp: string): string {
  if (stamp !== "dev") return stamp;
  const override = (globalThis as { __BATTLELAB_E2E_BUNDLE_VERSION__?: unknown })
    .__BATTLELAB_E2E_BUNDLE_VERSION__;
  return typeof override === "string" ? override : "dev";
}
