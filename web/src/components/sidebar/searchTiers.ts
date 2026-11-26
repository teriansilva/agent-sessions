import type { SessionMatch } from "../../types/api";

/** A text search's groups (#2373), in the order the server sorts them. */
export const TIER_LABEL: Record<SessionMatch["tier"], string> = {
  title: "Title",
  details: "In the details",
  conversation: "Found in the conversation",
};
