/** One ordering for every read of HQ's dispatches (#1837, Hermes 7623 finding 2; the Notifications
 *  view since #2146): its list polls and its single-dispatch lookups take a number from this ONE
 *  counter when their request STARTS. A list is the newer one only if its request started later — never because it arrived
 *  later, or because it is a different object. */
let last = 0;

export function nextDispatchRead(): number {
  last += 1;
  return last;
}

/** How often an open, visible Notifications view re-reads HQ's lines (Hermes 7619, finding 4). */
export const DISPATCH_POLL_MS = 15_000;
