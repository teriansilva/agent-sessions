/** The words for the step a pending ask is on (#1171) — from the server's own progress events, so
 *  they say what is actually happening. Its own module for the fast-refresh rule: `AskWorking.tsx`
 *  exports a component and nothing else. */
export type AskStep =
  // `hits`: keyword hits among the sessions searched (#2028); absent from older servers.
  | { step: "catalog"; sessions: number; missions: number; hits?: number; missionHits?: number }
  | { step: "content"; candidates: number }
  // A mission turn's own steps (#1224): what it is deciding, and what an instruction reads.
  | { step: "classify" }
  | { step: "instruct"; sessions: number }
  // A durable Ask turn's own steps (#1443): the model reading, and the read tool it is calling.
  | { step: "thinking"; round: number }
  | { step: "tool"; tool: string }
  // The turn watcher changed HQ's approach (#2136): its new strategy, in its own words.
  | { step: "replan"; text: string };

/** What each HQ tool does, in the operator's words (#2076): ONE table for the running row
 *  ("Reading a session's status…") and for the step list ("Reading a session's status"), covering
 *  EVERY tool the server offers — `tests/test_ask_stop.py` pins the keys against the server's tool
 *  list. `noun`: what the step's stored count counts, singular and plural. */
export const TOOL_LABELS: Record<string, { label: string; noun?: [string, string] }> = {
  find_sessions: { label: "Searching your sessions and missions", noun: ["match", "matches"] },
  artifacts_find: { label: "Searching the artifacts", noun: ["artifact", "artifacts"] },
  session_status: { label: "Reading a session's status", noun: ["session", "sessions"] },
  session_report: { label: "Reading a session's report", noun: ["report", "reports"] },
  session_brief: { label: "Reading a session's brief", noun: ["session", "sessions"] },
  // #2397: driving an API session. Each is one "Reply read" step in the list.
  session_turns: { label: "Reply read: a session's turns", noun: ["turn", "turns"] },
  wait_session: { label: "Waiting for a session's reply", noun: ["turn", "turns"] },
  list_sessions: { label: "Listing your sessions", noun: ["session", "sessions"] },
  list_missions: { label: "Reading your missions", noun: ["mission", "missions"] },
  mission_status: { label: "Reading a mission's status", noun: ["objective", "objectives"] },
  list_projects: { label: "Listing your projects", noun: ["project", "projects"] },
  list_folders: { label: "Listing your folders", noun: ["folder", "folders"] },
  list_agents: { label: "Listing the agents", noun: ["agent", "agents"] },
  list_agent_models: { label: "Listing an agent's models", noun: ["model", "models"] },
  list_specialists: { label: "Listing the specialists", noun: ["specialist", "specialists"] },
  specialist_detail: { label: "Reading a specialist" },
  // #1554: the read-only usage, fleet and settings views.
  agent_usage: { label: "Reading agent usage", noun: ["agent", "agents"] },
  fleet_status: { label: "Counting your sessions and missions" },
  read_settings: { label: "Reading your settings", noun: ["setting", "settings"] },
  // #1950: a mission's team.
  read_plan: { label: "Reading a mission's team", noun: ["step", "steps"] },
  read_pool: { label: "Reading who can take a step", noun: ["choice", "choices"] },
  assemble: { label: "Preparing the team for you to confirm" },
  assign: { label: "Preparing the assignment for you to confirm" },
  set_override: { label: "Preparing the override for you to confirm" },
  clear_override: { label: "Preparing the override's removal for you to confirm" },
  // HQ memory.
  list_memory_notebooks: { label: "Listing the memory notebooks", noun: ["notebook", "notebooks"] },
  search_memory: { label: "Searching memory", noun: ["note", "notes"] },
  read_memory: { label: "Reading a memory note", noun: ["note", "notes"] },
  remember_hq_memory: { label: "Writing a memory note" },
  propose_hq_memory_change: { label: "Preparing the memory change for you to confirm" },
  // #2249: the manual installed with this BattleLab.
  docs_search: { label: "Searching the manual", noun: ["section", "sections"] },
  docs_read: { label: "Reading the manual" },
  // #1611: the Library.
  list_playbooks: { label: "Listing your playbooks", noun: ["playbook", "playbooks"] },
  list_procedures: { label: "Listing the project's procedures", noun: ["procedure", "procedures"] },
  playbook_detail: { label: "Reading a playbook" },
  playbook_schema: { label: "Reading the playbook format" },
  list_skills: { label: "Listing your skills", noun: ["skill", "skills"] },
  skill_detail: { label: "Reading a skill" },
  inspect_skill_repo: { label: "Reading a skill repository", noun: ["skill", "skills"] },
  list_automations: { label: "Listing your automations", noun: ["automation", "automations"] },
  automation_detail: { label: "Reading an automation" },
  list_secrets: { label: "Listing your secrets' names", noun: ["secret", "secrets"] },
  propose_library: { label: "Preparing the Library change for you to confirm" },
  propose_secret: { label: "Preparing the secret change for you to confirm" },
  // #2163: project MCP servers.
  list_mcp_servers: { label: "Listing your MCP servers", noun: ["MCP server", "MCP servers"] },
  propose_mcp: { label: "Preparing the MCP server change for you to confirm" },
  // #1750, #1833: connected services.
  list_connectors: { label: "Listing your connectors", noun: ["connector", "connectors"] },
  connector_search: { label: "Searching a connected service", noun: ["result", "results"] },
  connector_read: { label: "Reading from a connected service" },
  connector_list: { label: "Listing a connected folder", noun: ["item", "items"] },
  list_recipients: { label: "Listing the recipients", noun: ["recipient", "recipients"] },
  propose_message: { label: "Preparing the message for you to send" },
  withdraw_card: { label: "Withdrawing a card" },
  // #2251: HQ's step list for a multi-part request.
  propose_steps: { label: "Sorting it into steps", noun: ["step", "steps"] },
  revise_steps: { label: "Changing the steps" },
  note: { label: "Keeping a note", noun: ["note", "notes"] },
  list_cards: { label: "Looking at the cards", noun: ["card", "cards"] },
  list_acts: { label: "Reading what HQ did", noun: ["act", "acts"] },
  // #2137: what waits on the Commander, and the bell.
  needs_you: { label: "Checking what needs you", noun: ["item", "items"] },
  notifications: { label: "Reading your notifications", noun: ["notification", "notifications"] },
  // #1559: staging a change for the operator to confirm.
  propose_session: { label: "Preparing the session for you to start" },
  propose_session_change: { label: "Preparing the change for you to confirm" },
  propose_project_change: { label: "Preparing the change for you to confirm" },
  propose_mission: { label: "Preparing the draft mission for you to confirm" },
  propose_relay: { label: "Preparing the relay to the mission" },
  // #2404: HQ's own shell, files and scripts.
  file_list: { label: "Listing a folder", noun: ["entry", "entries"] },
  file_read: { label: "Reading a file", noun: ["line", "lines"] },
  shell_run: { label: "Running a command" },
  file_write: { label: "Writing a file" },
  read_command_output: { label: "Reading a command's output" },
  // What HQ does itself, and how a turn ends.
  send_nudge: { label: "Sending a message", noun: ["session", "sessions"] },
  // #2102: HQ asks this screen to show something; what the screen did is the line under the answer.
  show: { label: "Putting it on your screen" },
  ask_operator: { label: "Asking you a question" },
  end_call: { label: "Ending the call" },
  // #2137: on a call, the decisions read back for the Commander to answer aloud.
  offer_decisions: { label: "Putting decisions to you" },
  offer_cards: { label: "Putting cards to you again" }, // #2389
};

/** A step's words in the list: the tool's label, or a lookup for a tool this build does not know
 *  (a newer server). */
export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool]?.label ?? "Looking it up";
}

const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

export function askStepLabel(step: AskStep | null): string {
  if (!step || step.step === "classify") return "Reading your question…";
  if (step.step === "thinking")
    return step.round === 0 ? "Reading your question…" : "Putting the answer together…";
  if (step.step === "tool") return `${toolLabel(step.tool)}…`;
  if (step.step === "replan") return `Changing approach: ${step.text}`;
  if (step.step === "instruct") {
    return step.sessions
      ? `Drafting the instruction for ${plural(step.sessions, "session", "sessions")}…`
      : "Drafting the instruction…";
  }
  if (step.step === "catalog" && step.hits !== undefined) {
    // What was actually searched (#2028): the slice is keyword hits padded with recent sessions
    // up to a cap, so its size is never "N matching sessions".
    const hits = step.hits;
    const missionHits = step.missionHits ?? 0;
    const parts = [
      hits > 0 ? plural(hits, "matching session", "matching sessions") : "",
      missionHits > 0 ? plural(missionHits, "matching mission", "matching missions") : "",
    ].filter(Boolean);
    if (parts.length === 0) return "Searching your recent sessions…";
    const padded = step.sessions > hits || step.missions > missionHits;
    return `Searching ${parts.join(" and ")}${padded ? " and your recent ones" : ""}…`;
  }
  if (step.step === "catalog") {
    const what = [
      step.sessions ? plural(step.sessions, "session", "sessions") : "",
      step.missions ? plural(step.missions, "mission", "missions") : "",
    ]
      .filter(Boolean)
      .join(" and ");
    return `Searching ${what || "your work"}…`;
  }
  return step.candidates
    ? `Checking against ${plural(step.candidates, "transcript", "transcripts")}…`
    : "Checking against the transcripts…";
}
