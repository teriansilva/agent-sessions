// lucide-style stroked icons (currentColor), square joins kept simple.
const P = {
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  rail: '<rect width="18" height="18" x="3" y="3"/><path d="M15 3v18M17.5 8h1M17.5 12h1M17.5 16h1"/>',
  more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  cr: '<path d="m9 18 6-6-6-6"/>',
  cu: '<path d="m18 15-6-6-6 6"/>',
  cd: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  down: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  min: '<path d="M5 12h14"/>',
  max: '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
};
function I(name, extra = '') {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" ${extra}>${P[name]}</svg>`;
}
