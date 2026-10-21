import { Component, type ReactNode } from "react";

/** Catches the second-failure case from `lazyWithReload` (and any non-chunk render error from
 *  the lazy subtree) so the user sees a recoverable message instead of a blank route. Scoped
 *  per Suspense block — wrap each one independently. (#160) */
export class ChunkErrorBoundary extends Component<
  { children: ReactNode; fallback?: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        this.props.fallback ?? (
          <div className="tr-overview tr-ov-state">
            We couldn’t load this part of the app.{" "}
            <button type="button" onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}
