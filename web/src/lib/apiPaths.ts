/** API route paths named in more than one file: the client and the browser tests that mock it.
 *
 *  One exported string per route, so a renamed route is one edit and a spec can never mock a path
 *  the app no longer calls. Kept free of imports so a Playwright spec can load it without pulling in
 *  the browser-only client. */

/** `POST` — what a direction would type, filled with example facts (#983 P2). Read-only. */
export const DIRECTION_PREVIEW_PATH = "/api/mission-directions/preview";

/** The automations API (#1201): list/create at the root, one automation at `/{id}`. */
export const AUTOMATIONS_API = "/api/automations";

/** `GET` — `{origins: {key: {automation_id, name, …}}}`: which sessions and missions an automation
 *  started, for the origin badges. */
export const AUTOMATION_ORIGINS_API = `${AUTOMATIONS_API}/origins`;

/** Library → Webhooks (#2221): list/create at the root, one webhook at `webhookApiPath(id)`. */
export const WEBHOOKS_API = "/api/webhooks";
export const webhookApiPath = (id: string) => `${WEBHOOKS_API}/${encodeURIComponent(id)}`;

/** `GET` — Library → Actions (#2248 P1): every action an automation can take, with its manifest
 *  facts, what it needs and how many automations use it. Read-only; the wizard picks from it too. */
export const ACTIONS_API = "/api/actions";

/** `POST` — stop one running HQ turn (#2076). It names the turn, so it can never end another. */
export const askStopTurnPath = (cid: string, turnId: string) =>
  `/api/ask/conversations/${encodeURIComponent(cid)}/turns/${encodeURIComponent(turnId)}/stop`;

/** Project MCP servers (#2163). A review and an install are operations (P1b): the POST names the
 *  operation (`request_id`) and answers 202; the client polls `mcpOperationPath` until it ends. */
export const MCP_SERVERS_API = "/api/mcp/servers";
export const mcpReviewPath = (sid: string) => `${MCP_SERVERS_API}/${encodeURIComponent(sid)}/review`;
export const mcpInstallPath = (sid: string) => `${MCP_SERVERS_API}/${encodeURIComponent(sid)}/install`;
export const MCP_OPERATIONS_API = "/api/mcp/operations";
export const mcpOperationPath = (requestId: string) => `${MCP_OPERATIONS_API}/${encodeURIComponent(requestId)}`;

/** `POST` — take back one queued HQ question (#2103). It names the turn, like Stop. */
export const askWithdrawTurnPath = (cid: string, turnId: string) =>
  `/api/ask/conversations/${encodeURIComponent(cid)}/turns/${encodeURIComponent(turnId)}/withdraw`;

/** `GET` — HQ's tasks (#2103): the turns running and waiting, the ones parked on a question and
 *  the last settled ones. Stored first lines only. */
export const HQ_TASKS_API = "/api/hq/tasks";

/** `POST` — stop every running HQ turn and withdraw every queued question (#2103). */
export const HQ_TASKS_STOP_ALL_API = "/api/hq/tasks/stop-all";
