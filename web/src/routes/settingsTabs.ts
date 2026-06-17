/** The Settings tabs (#357 Phase 1) — data-driven so adding a tab later (e.g. the #289
 *  Operations panel) is one entry. `/settings/:tab` is the single canonical deep-link form:
 *  bare `/settings` redirects to the first tab, and an unknown `:tab` falls back to it
 *  (no 404). Lives outside Settings.tsx so non-component exports don't break fast refresh. */
export const SETTINGS_TABS = [
  { id: "appearance", label: "Appearance" },
  { id: "projects", label: "Projects" },
  { id: "ai-review", label: "AI" },
  { id: "security", label: "Security" },
  { id: "system", label: "System" },
  { id: "maintenance", label: "Maintenance" },
  { id: "about", label: "About" },
] as const;

export type SettingsTabId = (typeof SETTINGS_TABS)[number]["id"];

export const DEFAULT_SETTINGS_TAB: SettingsTabId = SETTINGS_TABS[0].id;

export function isSettingsTab(tab: string | undefined): tab is SettingsTabId {
  return SETTINGS_TABS.some((t) => t.id === tab);
}
