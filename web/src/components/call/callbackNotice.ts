/** A "call me back" notice (#2341 §4): the server's action id prefix
 *  (`voice_callbacks.ACTION_PREFIX`) and a session to call. */
import type { PulseNotification } from "../../types/api";

export function isCallbackNotice(n: Pick<PulseNotification, "action_id" | "session_id">): boolean {
  return typeof n.action_id === "string" && n.action_id.startsWith("voicecb:") && !!n.session_id;
}
