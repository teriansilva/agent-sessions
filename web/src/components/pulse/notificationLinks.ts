/** Where a notification row's Open goes, and what it says (#2146: shared by the bell's rows and
 *  HQ's Notifications view, so both point at the same place). */
import { remotePath } from "../../lib/federation";
import { NOTES_QUERY, peerNotice } from "../../lib/federationTogether";
import { MISSION_PATH } from "../../lib/missionLink";
import { automationPath } from "../../lib/routes";
import type { PulseNotification } from "../../types/api";

/** An automation's failure episode (`automation:<id>:<run>`, #1201) opens that automation's run
 *  history — the same link the server puts in its push (`notifications._link`). */
const AUTOMATION_ACTION_RE = /^automation:([a-z0-9]{1,64}):/;

/** Deep link for a notification: the session it concerns, an automation's run history, a session
 *  another instance shares (#2062: the row names the peer and the grant, as the server's own push
 *  link does, `notifications._link`), or mission control when it has none of them. A row about a
 *  note opens that grant's thread. */
export function targetPath(n: PulseNotification): string {
  const auto = !n.session_id && AUTOMATION_ACTION_RE.exec(n.action_id ?? "");
  if (auto) return automationPath(auto[1]);
  const peer = peerNotice(n);
  if (!n.session_id && peer?.kind === "remote")
    return `${remotePath(peer.peerId, peer.grantId)}${peer.what === "note" ? `?${NOTES_QUERY}=1` : ""}`;
  if (!n.session_id) return MISSION_PATH;
  const uuid = n.session_id.slice(n.session_id.indexOf(":") + 1);
  const path = `/s/${encodeURIComponent(n.engine)}/${encodeURIComponent(uuid)}`;
  return peer?.kind === "note" ? `${path}?${NOTES_QUERY}=${encodeURIComponent(peer.grantId)}` : path;
}

/** The long Open label for a target, as HQ's dispatches have always said it. */
export function openLabel(link: string): string {
  if (link.startsWith("/s/") || link.startsWith("/remote/")) return "Open session";
  if (link.startsWith("/mission/automations/")) return "Open automation";
  return "Open mission";
}
