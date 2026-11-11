/** One ordering for every read of HQ's Dispatches list (#1837, Hermes 7623 finding 2): the page's
 *  poll and the Dispatches view's own reads take a number from this ONE counter when their request
 *  STARTS. A list is the newer one only if its request started later — never because it arrived
 *  later, or because it is a different object. */
let last = 0;

export function nextDispatchRead(): number {
  last += 1;
  return last;
}
