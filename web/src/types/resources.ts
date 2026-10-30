export type ResourceValues = {
  console_tasks: number;
  api_tasks: number;
  library_threads: number;
  api_memory_gib: number;
  max_sessions: number;
};
/** What the host offers and what the recommendation made of it (#1819). A null was unreadable. */
export type SessionCapacityFacts = {
  memory_bytes: number | null;
  cpus: number | null;
  reserved_bytes: number | null;
  by_memory: number | null;
  by_cpu: number | null;
  limited_by: "memory" | "cpu" | "unknown";
  recommended: number;
};
export type RunningSessionRow = {
  id: string;
  title: string;
  engine: string;
  project: string;
  last_mtime: number;
  working: boolean;
  mission: { id: string; title: string; state: string } | null | undefined;
};
/** `GET /api/sessions/capacity`: the limit dialog's list. */
export type SessionCapacity = {
  limit: number;
  recommended: number;
  source: string;
  readable: boolean;
  running: number | null;
  others: number;
  sessions: RunningSessionRow[];
};
export type ResourceSettings = {
  values: ResourceValues;
  recommended: ResourceValues;
  bounds: Record<keyof ResourceValues, { min: number; max: number }>;
  sources: Record<keyof ResourceValues, string>;
  console_tasks_max: string;
  capacity: SessionCapacityFacts;
  running: number | null;
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
