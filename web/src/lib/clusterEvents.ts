/** Session clusters changed from somewhere in this tab (#1997): a session was moved, or a cluster
 *  was renamed, kept, merged, dissolved or re-clustered. The session list listens and re-reads,
 *  because a patch cannot move a row out of a `cluster` filter or correct the tree's counts.
 *
 *  Its own module, like `missionEvents`, so a listener needs the event name and not the API
 *  client. */
export const CLUSTERS_CHANGED_EVENT = "agent-sessions:clusters-changed";

/** Never throws: the server has already recorded the change by the time this runs. */
export function announceClustersChanged(): void {
  try {
    window.dispatchEvent(new CustomEvent(CLUSTERS_CHANGED_EVENT));
  } catch {
    // no-op — the change stands whether or not anything is listening
  }
}
