import type { useNavigate } from "react-router-dom";
import type { WindowSeed } from "../components/overview/useWorkspace";
import type { FreshSession } from "./termUrl";

/** What the New session form launched (#1648): the session's window seed and, for a console
 *  launch, its fresh-launch params. The form EMITS it; its host (the `/new` page or the map's
 *  modal) decides where it opens. */
export interface LaunchOutcome {
  seed: WindowSeed;
  fresh?: FreshSession;
}

/** Full screen: the session's own route, carrying `fresh` when there is one. */
export function openFullScreen(
  navigate: ReturnType<typeof useNavigate>,
  { seed, fresh }: LaunchOutcome,
): void {
  const to = `/s/${seed.engine}/${seed.id}`;
  if (fresh) navigate(to, { state: { fresh } });
  else navigate(to);
}
