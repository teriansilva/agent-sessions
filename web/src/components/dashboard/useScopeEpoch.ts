import { useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";

/** The hard scope as a restart signal for a retained dashboard read (#1223).
 *
 *  Returns a number that advances only when a KNOWN scope moves. Config answering (null → known)
 *  is not a move: the server applied that one scope all along, so an in-flight read stays valid.
 *  On a move, `gen` is bumped at once — a read from the old scope resolving before the re-render
 *  must not paint — and `onMove` drops what was painted, so the source goes cold rather than
 *  showing another boundary's rows while it re-reads. */
export function useScopeEpoch(
  scopeKey: string | null,
  genRef: MutableRefObject<number>,
  onMove: () => void,
): number {
  const [epoch, setEpoch] = useState(0);
  const paintedRef = useRef(scopeKey);
  const onMoveRef = useRef(onMove);
  useEffect(() => {
    onMoveRef.current = onMove;
  });
  useEffect(() => {
    const painted = paintedRef.current;
    if (painted !== null && scopeKey !== null && painted !== scopeKey) {
      // Defence in depth: the epoch re-render's cleanup bumps it too, but a promise settling
      // between this effect and that commit would otherwise still count as current.
      genRef.current++;
      onMoveRef.current();
      setEpoch((n) => n + 1);
    }
    if (scopeKey !== null) paintedRef.current = scopeKey;
  }, [scopeKey, genRef]);
  return epoch;
}
