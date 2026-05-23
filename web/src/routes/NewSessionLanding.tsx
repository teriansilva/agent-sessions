/** Landing page at "/" — no session selected. The agent picker + project picker
 *  (Phase 6) render here; for now a placeholder that establishes the route so the
 *  app never auto-resumes a session (you choose, or open one from the list). */
export function NewSessionLanding() {
  return (
    <div className="landing">
      <h1>Start a new session</h1>
      <p>Pick an agent + project to begin, or open an existing session from the list.</p>
      <p className="hint">(agent picker — Phase 6)</p>
    </div>
  );
}
