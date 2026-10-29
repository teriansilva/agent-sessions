/** HQ's conversation LIST, shared by every console and the HQ page (#1776).
 *
 *  It was `AskConsole`'s private state. The HQ page's left column and the sidebar's switcher show
 *  the same list, so a send, rename or delete in either must show in both — one store, read once
 *  here, the way `askUnreadStore` shares the unread dot.
 *
 *  **The generation guard (Hermes on #1564).** Every read takes a generation, and only the newest
 *  read's answer applies. A delete bumps the generation BEFORE it removes the row, so a list read
 *  that was sent before the delete — and answers with the deleted conversation still in it — lands
 *  nowhere, in the sidebar and on the page alike. A failed read keeps what is shown; the first one
 *  that fails shows an empty list rather than "loading" forever.
 *
 *  `null` means "not read yet". The store lives for the page's lifetime; `_resetForTests` clears
 *  it between unit tests. */
import { useSyncExternalStore } from "react";

import { api } from "../../lib/api";
import type { AskConversationSummary } from "../../types/api";

let rows: AskConversationSummary[] | null = null;
let gen = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of listeners) fn();
}

function set(next: AskConversationSummary[]): void {
  rows = next;
  emit();
}

/** Read the list again. Resolves once this read has applied (or lost to a newer one). */
export async function refreshConversations(): Promise<void> {
  const g = ++gen;
  try {
    const r = await api.askConversations();
    if (g === gen) set(r.conversations ?? []);
  } catch {
    // Keep what is shown; a list never read yet shows as empty, not as "loading" forever.
    if (g === gen && rows === null) set([]);
  }
}

/** A conversation was deleted: drop its row at once, and fence off every read already in flight
 *  (each predates the delete and may still list it). */
export function conversationRemoved(id: string): void {
  gen += 1;
  if (rows) set(rows.filter((c) => c.id !== id));
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function snapshot(): AskConversationSummary[] | null {
  return rows;
}

/** The kept conversations, latest first; `null` until the first read lands. */
export function useAskConversations(): AskConversationSummary[] | null {
  return useSyncExternalStore(subscribe, snapshot, () => null);
}

/** Bumped whenever HQ's page shows or changes a conversation (#1776). The kept sidebar reads its
 *  conversation again on reopening only if this moved while it was hidden — the page may have
 *  added turns to it — so a sidebar used alone behaves exactly as before. */
let pageEpoch = 0;
export function pageTouched(): void {
  pageEpoch += 1;
}
export function pageEpochNow(): number {
  return pageEpoch;
}

export function _resetForTests(): void {
  rows = null;
  gen += 1;
}
