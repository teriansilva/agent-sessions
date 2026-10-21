/** View state for one app visit. No server preference or cross-user/device persistence. */
import { createContext, useContext, useEffect, useState } from "react";

export const SectionStateContext = createContext<Map<string, unknown> | null>(
  null,
);

export function useSectionState<T>(key: string, initial: T) {
  const memory = useContext(SectionStateContext);
  const [value, setValue] = useState<T>(() =>
    memory?.has(key) ? (memory.get(key) as T) : initial,
  );
  useEffect(() => {
    memory?.set(key, value);
  }, [memory, key, value]);
  return [value, setValue] as const;
}
