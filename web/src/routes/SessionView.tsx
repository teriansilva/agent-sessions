import { useParams } from "react-router-dom";
import { Terminal } from "../components/terminal/Terminal";

/** Session view at "/s/:engine/:id" — the URL is the single source of truth for which
 *  session is open (deep-linkable, refresh-safe). Renders the live terminal; the `key`
 *  forces a clean remount when the route switches to a different session. */
export function SessionView() {
  const { engine, id } = useParams<{ engine: string; id: string }>();
  if (!engine || !id) return null;
  return <Terminal key={`${engine}:${id}`} engine={engine} id={id} />;
}
