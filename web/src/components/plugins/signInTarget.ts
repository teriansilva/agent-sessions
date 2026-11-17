import { api } from "../../lib/api";

/** Which sign-in a terminal drives: the socket it attaches to, and the authenticated cancel that
 *  ends it when this terminal closes while between sockets. A plugin candidate's sign-in is keyed
 *  by its operation (`pluginSignIn`); an agent account's by the account (#1820). */
export interface SignInTarget {
  /** Path of the websocket, e.g. `/ws/plugins/signin/<operation>`. Identity of the attempt. */
  socketPath: string;
  cancel: () => Promise<unknown>;
  /** Deliver the address a loopback sign-in left the browser on (#2106). */
  callback: (url: string) => Promise<unknown>;
}

export function pluginSignIn(operation: string): SignInTarget {
  return {
    socketPath: `/ws/plugins/signin/${encodeURIComponent(operation)}`,
    cancel: () => api.pluginCancel(operation),
    callback: url => api.pluginSigninCallback(operation, url),
  };
}
