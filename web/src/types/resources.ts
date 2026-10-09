export type ResourceValues = {
  console_tasks: number;
  api_tasks: number;
  library_threads: number;
  api_memory_gib: number;
};
export type ResourceSettings = {
  values: ResourceValues;
  recommended: ResourceValues;
  bounds: Record<keyof ResourceValues, { min: number; max: number }>;
  sources: Record<keyof ResourceValues, string>;
  console_tasks_max: string;
  library_overrides: { name: string; value: string }[];
  notice: string | null;
};
export type ResourceCounter = {
  group: string;
  current: number | null;
  maximum: number | null;
  unlimited: boolean;
  denied: number | null;
};
export type ResourceUsage = {
  observed_at: number;
  console_containment: "disabled" | "unverified" | "verified" | "unavailable";
  groups: {
    unit: string;
    kind: "api" | "console";
    own: ResourceCounter;
    ancestors: ResourceCounter[];
    severity: "normal" | "warning" | "critical" | "unknown";
    pressure: number | null;
    headroom: number | null;
    incomplete: boolean;
  }[];
  truncated: boolean;
  error: string | null;
};
export type Resources = { settings: ResourceSettings; usage: ResourceUsage };
