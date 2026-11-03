import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import type { PluginCatalog } from "../../types/plugins";
import { signinPath } from "../agents/agentStatus";
import chat from "../chat/ChatPane.module.css";

/** Sign in, on a turn that failed because the agent's login is missing or expired (`remedy:
 *  signin`): Settings → Agents setup for `agent` (the console agent whose login this session
 *  uses), at its waiting candidate generation's sign-in when there is one. Until the catalog
 *  answers, or when it cannot be read, the agent's own setup. */
export function SigninLink({ agent, label }: { agent: string; label: string }) {
  const [catalog, setCatalog] = useState<PluginCatalog | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .plugins()
      .then((value) => alive && setCatalog(value))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [agent]);
  const to = signinPath(agent, catalog);
  // No waiting copy to sign in with (#1846): the link opens setup, where a new copy is reviewed
  // first. Say so, and that nothing running is disabled: the sign-in refreshes the host's own
  // vendor login, which the installed copy keeps using.
  const viaSetup = catalog !== null && !to.includes("generation=");
  return (
    <>
      <Link className={chat.send} to={to} data-testid="structured-signin">
        Sign in to {label}
      </Link>
      {viaSetup && (
        <p className={chat.note} data-testid="structured-signin-via-setup">
          No spare copy of {label} is installed, so this opens its setup: review a new copy, then
          sign in there. The installed copy and its sessions keep running; the new sign-in is the
          one they use.
        </p>
      )}
    </>
  );
}
