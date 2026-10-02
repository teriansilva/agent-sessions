/** Band names, worded exactly as the card grid worded them (#878).
 *
 * Colour alone is not an accessible state. When the grid dropped its section headings (#750) the
 * band moved onto the LED's accessible name, and dropping the grid must not drop that with it —
 * so this map is shared rather than restated, and lives outside a component file so importing it
 * does not break fast refresh.
 */
export const BAND_LABEL: Record<string, string> = {
  needs_you: "Needs you",
  in_flight: "In flight",
  recently_active: "Recently active",
  idle: "Idle",
};
