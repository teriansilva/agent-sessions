/** Settings sections (#956): the single source of truth for what Settings contains, how it is
 *  grouped, and the URL each page lives at. The desktop sidebar and the phone index both render
 *  from this list, so adding a page is one entry here plus its body in Settings.tsx.
 *
 *  `/settings/:section` is the one canonical deep-link form. Every in-app link and every e2e
 *  spec builds it with `settingsPath()` rather than a string literal, so the next rename is a
 *  one-line change. Lives outside Settings.tsx so non-component exports don't break fast refresh
 *  (and so e2e specs can import it without pulling in React). */

export const SETTINGS_PATH = "/settings";

export const SETTINGS_GROUPS = [
  { id: "general", label: "General" },
  { id: "ai", label: "AI" },
  { id: "agents", label: "Agents" },
  { id: "system", label: "System" },
  { id: "about", label: "About" },
] as const;

export type SettingsGroupId = (typeof SETTINGS_GROUPS)[number]["id"];

export const SETTINGS_SECTIONS = [
  { id: "appearance", label: "Appearance", group: "general" },
  { id: "session-defaults", label: "Session defaults", group: "general" },
  { id: "projects", label: "Projects", group: "general" },
  { id: "ai-endpoint", label: "Endpoint & model", group: "ai" },
  { id: "ai-session-review", label: "Session review", group: "ai" },
  { id: "ai-auto-sort", label: "Auto-sort", group: "ai" },
  { id: "ai-mission-control", label: "Mission control", group: "ai" },
  { id: "ai-prompts", label: "Prompts", group: "ai" },
  { id: "ai-activity", label: "Activity", group: "ai" },
  // AGENTS (#853 P4, #1128): the roster keeps the `/settings/agents` URL it had as "Agents &
  // usage"; each agent's own page is `/settings/agents/<id>` (`agentPath`), not a registry entry.
  { id: "agents", label: "Roster", group: "agents" },
  { id: "agents-defaults", label: "Defaults", group: "agents" },
  { id: "security", label: "Security", group: "system" },
  { id: "updates", label: "Updates", group: "system" },
  { id: "analytics", label: "Usage analytics", group: "system" },
  { id: "system", label: "Host", group: "system" },
  { id: "maintenance", label: "Maintenance", group: "system" },
  { id: "about", label: "About", group: "about" },
] as const satisfies readonly {
  id: string;
  label: string;
  group: SettingsGroupId;
}[];

export type SettingsSectionId = (typeof SETTINGS_SECTIONS)[number]["id"];
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export const DEFAULT_SETTINGS_SECTION: SettingsSectionId =
  SETTINGS_SECTIONS[0].id;

export function isSettingsSection(
  id: string | undefined,
): id is SettingsSectionId {
  return SETTINGS_SECTIONS.some((s) => s.id === id);
}

export function settingsSection(id: SettingsSectionId): SettingsSection {
  return SETTINGS_SECTIONS.find((s) => s.id === id)!;
}

export function settingsGroup(id: SettingsGroupId) {
  return SETTINGS_GROUPS.find((g) => g.id === id)!;
}

/** Sections whose URL is not simply `/settings/<id>`. Defaults lives UNDER the roster
 *  (`/settings/agents/defaults`), beside each agent's own page. */
const SECTION_PATHS: Partial<Record<SettingsSectionId, string>> = {
  "agents-defaults": `${SETTINGS_PATH}/agents/defaults`,
};

/** The URL of a section, or of the Settings root (`/settings`) when none is given. `hash` may be
 *  passed with or without its leading `#`. */
export function settingsPath(id?: SettingsSectionId, hash?: string): string {
  const base = id
    ? (SECTION_PATHS[id] ?? `${SETTINGS_PATH}/${id}`)
    : SETTINGS_PATH;
  if (!hash) return base;
  return `${base}${hash.startsWith("#") ? hash : `#${hash}`}`;
}

/** The second segment under `/settings/agents/` that is the Defaults page, not an agent id. */
export const AGENT_DEFAULTS_SEGMENT = "defaults";

/** One agent's own page (#853 P4): `/settings/agents/<id>`. */
export function agentPath(engineId: string): string {
  return `${SETTINGS_PATH}/agents/${encodeURIComponent(engineId)}`;
}

/** The hash that deep-links one prompt row on the Prompts page: `#prompt-<id>`. */
export const PROMPT_HASH_PREFIX = "#prompt-";

/** Link to one prompt in the catalog — what the "edit it in Prompts" links in the feature
 *  pages point at. */
export function promptPath(promptId: string): string {
  return settingsPath("ai-prompts", `${PROMPT_HASH_PREFIX}${promptId}`);
}

/** Where a pre-#956 section id lives now, or `null` when `id` is not a legacy id.
 *
 *  `ai-review` was the whole AI tab. Most links into it were about the endpoint, so that is where
 *  it lands — except a `#prompt-<id>` deep link, which keeps pointing at the prompt it named. */
export function legacySettingsTarget(
  id: string | undefined,
  hash: string,
): string | null {
  if (id !== "ai-review") return null;
  return hash.startsWith(PROMPT_HASH_PREFIX)
    ? settingsPath("ai-prompts", hash)
    : settingsPath("ai-endpoint");
}
