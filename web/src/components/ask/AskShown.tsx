import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useScreen, useScreenOutcomes } from "../../app/screenControl";
import { screenLines, type ScreenOpen, type ScreenShow } from "../../app/screenDirective";
import action from "../ui/actionButton.module.css";
import { matchRoute } from "./askMatches";
import { ASK_DRAWER_MQ, useAskPanel } from "./askPanel";
import s from "./AskShown.module.css";

/** What HQ put on the Commander's screen in one turn (#2102), as quiet rows directly under the
 *  answer: never a card, never an alert. The words come from THIS device's own outcome for each
 *  directive — "Shown" only where the executor applied it here; read back from history (a
 *  reload, another device, a page that was closed) the same record reads "Asked to show", which
 *  claims nothing. A record is never executed by being rendered: only Open, a tap, opens it. */
export function AskShown({ shows }: { shows: ScreenShow[] }) {
  const screen = useScreen();
  const outcomes = useScreenOutcomes();
  const navigate = useNavigate();
  const panel = useAskPanel();
  const lines = useMemo(
    () => screenLines(shows, (id) => outcomes.get(id)),
    [shows, outcomes],
  );
  if (!lines.length) return null;
  const open = (to: ScreenOpen) => {
    if (screen) screen.open(to);
    else if (to.type === "route") navigate(to.to);
    else if (to.sessions.length) navigate(matchRoute(to.sessions[0].key));
    // On a phone HQ is a drawer over the page: it gives way to what was opened.
    if (window.matchMedia?.(ASK_DRAWER_MQ).matches) panel.close();
  };
  const plate = `${action.ghost} ${action.compact} ${s.open}`;
  return (
    <>
      {lines.map((line) => (
        <p
          key={`${line.kind}:${line.lead}${line.strong}${line.tail}`}
          className={`${s.shown}${line.kind === "shown" ? "" : ` ${s.neutral}`}`}
          data-testid="ask-shown"
          data-kind={line.kind}
        >
          <span className={s.rec}>
            {line.lead}
            {line.strong ? <b>{line.strong}</b> : null}
            {line.tail}
          </span>
          {line.open === null ? null : line.open.type === "route" ? (
            <Link className={plate} to={line.open.to} data-testid="ask-shown-open">
              Open
            </Link>
          ) : (
            <button
              type="button"
              className={plate}
              onClick={() => open(line.open!)}
              data-testid="ask-shown-open"
            >
              Open
            </button>
          )}
        </p>
      ))}
    </>
  );
}
