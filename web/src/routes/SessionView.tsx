import { useLocation, useParams } from "react-router-dom";
import { Terminal } from "../components/terminal/Terminal";
import type { FreshSession } from "../lib/termUrl";

/** Session view at "/s/:engine/:id" — the URL is the single source of truth for which
 *  session is open (deep-linkable, refresh-safe). When arrived at from the new-session
 *  landing, router state carries the fresh-launch params (cwd + bypass) so the terminal
 *  opens it with ?new=1; a direct deep-link / reload has no state → a plain attach. */
export function SessionView() {
  const { engine, id } = useParams<{ engine: string; id: string }>();
  const location = useLocation();
  const fresh = (location.state as { fresh?: FreshSession } | null)?.fresh;
  if (!engine || !id) return null;
  return <Terminal key={`${engine}:${id}`} engine={engine} id={id} fresh={fresh} />;
}
