import { useEffect, useState } from "react";
import { api } from "../../lib/api";

/** Which local copies were created from a file (`"<kind>:<id>"`). Optional data: a failed or
 *  malformed read is "none", never an error on the page. */
export function useShareImports(reloadKey = 0): Set<string> {
  const [keys, setKeys] = useState<Set<string>>(new Set());
  useEffect(() => {
    let live = true;
    Promise.resolve()
      .then(() => api.shareImports())
      .then((value) => {
        const imports = value && typeof value === "object" ? (value as { imports?: unknown }).imports : null;
        if (live && imports && typeof imports === "object" && !Array.isArray(imports))
          setKeys(new Set(Object.keys(imports)));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [reloadKey]);
  return keys;
}
