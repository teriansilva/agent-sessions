import { api } from "../../lib/api";
import type { SignInTarget } from "../plugins/signInTarget";

/** The sign-in socket of one agent account (#1820), and the cancel that ends it when the terminal
 *  closes while between sockets. */
export function accountSignIn(engine: string, account: string): SignInTarget {
  return {
    socketPath: `/ws/agent-accounts/${encodeURIComponent(engine)}/${encodeURIComponent(account)}/signin`,
    cancel: () => api.cancelAgentAccountSignin(engine, account),
    callback: url => api.agentAccountSigninCallback(engine, account, url),
  };
}
