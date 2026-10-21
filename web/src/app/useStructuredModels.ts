import { useEffect, useSyncExternalStore } from "react";
import {
  structuredModelsStore,
  type StructuredModelsSnapshot,
} from "../lib/structuredModelsStore";

/** An API client's model list (#1503): paints the cached list at once and revalidates behind it
 *  on every mount and every engine change. `null` engine (a console agent) asks nothing. */
export function useStructuredModels(engine: string | null): StructuredModelsSnapshot {
  const snap = useSyncExternalStore(structuredModelsStore.subscribe, () =>
    structuredModelsStore.get(engine ?? ""),
  );
  useEffect(() => {
    if (engine) void structuredModelsStore.revalidate(engine);
  }, [engine]);
  return snap;
}

/** Ask for every listed API client's models now, so switching between them paints at once. */
export function usePrefetchStructuredModels(engines: string[]): void {
  const key = engines.join("\n");
  useEffect(() => {
    if (key) key.split("\n").forEach((e) => void structuredModelsStore.revalidate(e));
  }, [key]);
}
