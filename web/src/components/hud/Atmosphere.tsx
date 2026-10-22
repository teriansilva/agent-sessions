// The atmosphere behind the whole app (NIGHTJAR #1531, docs/design.md §8): hex grid, scanlines,
// noise grain, the sweep band and the vignette (nightjar.css `.nj-atmo`). It is the app's ONE
// backdrop: the #211 data-flow trails and their 48 px square grid are retired here (the map's
// tactical grid is the map's own). Everything follows the ONE motion mode (theme/motion.ts):
// Full animates on dark; Reduced, Off, a hidden tab or 10 idle minutes leave a still frame. Light
// shows the static hex grid only.
//
// Deliberately cosmetic and non-interactive (pointer-events: none, behind everything). The
// standalone connect shell still runs hud/dataFlow.ts's trails until its own NIGHTJAR pass.
export function Atmosphere() {
  return (
    <div className="nj-atmo" aria-hidden="true" data-testid="nj-atmo">
      <i className="nj-hex" />
      <i className="nj-scan" />
      <i className="nj-noise" />
      <i className="nj-sweep" />
      <i className="nj-vig" />
    </div>
  );
}
