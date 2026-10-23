// Settings B states for the NIGHTJAR sweep (#1574): agents, system and about. Every secret here is a
// FAKE fixture value (the 2FA key and recovery codes, share ids) — never a real credential.

// Capture time: the pages count expiries and elapsed time from the browser's clock.
const NOW = Math.floor(Date.now() / 1000);

/** The installer catalog, read and signed (the roster's catalog line, the Add agent wizard). */
export const PLUGINS = {
  feed: {
    state: "ready",
    source: "bundled",
    error: null,
    stale: false,
    sequence: null,
    digest: "a".repeat(64),
    bundled_digest: "a".repeat(64),
    release_version: "0.20.0",
    definitions_url: "https://example.invalid/recipes",
    history_url: "https://example.invalid/recipes/history",
    updates_url: "https://example.invalid/releases",
    refresh: { automatic: true, last_attempt: NOW - 7200, last_success: NOW - 7200, error: null },
  },
  catalog: [
    {
      manifest: { identity: { id: "sample-agent", label: "Sample agent", version: "1.2.3", publisher: "BattleLab" } },
      digest: "c".repeat(64),
      installable: true,
      source: "bundled",
      reason: null,
    },
    {
      manifest: { identity: { id: "relay-agent", label: "Relay agent", version: "0.4.0", publisher: "BattleLab" } },
      digest: "d".repeat(64),
      installable: true,
      source: "bundled",
      reason: null,
    },
  ],
  plugins: [],
  operations: [],
  roster_generation: 1,
  roster_revision: null,
};

export const UPDATE_SETTINGS = { auto_update: true, channel: "stable", last_auto: { ts: NOW - 86400, result: "up to date" } };
/** The installer mid-run: step 2 of 4 (the progress panel and its Acquiring strip). */
export const UPDATE_RUNNING = {
  state: "running",
  steps: 4,
  step: "build",
  step_index: 2,
  label: "Building the web app",
  started_at: NOW - 95,
  elapsed_s: 95,
  last_duration_s: 240,
};
export const UPDATE_IDLE = { state: "idle", steps: 4 };

export const SYSTEM = {
  os: "Linux",
  platform: "linux-x86_64",
  cpus: 16,
  python: "3.12",
  hostname: "battlelab-host",
};

export const RESOURCES = {
  settings: {
    values: { console_tasks: 4096, api_tasks: 4096, library_threads: 8, api_memory_gib: 8 },
    recommended: { console_tasks: 4096, api_tasks: 4096, library_threads: 8, api_memory_gib: 8 },
    bounds: {
      console_tasks: { min: 256, max: 16384 },
      api_tasks: { min: 256, max: 16384 },
      library_threads: { min: 1, max: 64 },
      api_memory_gib: { min: 1, max: 1024 },
    },
    sources: {
      console_tasks: "recommended",
      api_tasks: "recommended",
      library_threads: "recommended",
      api_memory_gib: "recommended",
    },
    console_tasks_max: "4096",
    library_overrides: [],
  },
  usage: { observed_at: NOW, console_containment: "verified", truncated: false, error: null, groups: [] },
};

/** Two live links (Settings → Sharing). Ids are fixtures, never a link token. */
export const SHARES = {
  available: true,
  reason: null,
  shares: [
    {
      share_id: "shr_fixture_0001",
      session_key: "claude:7c1e1a00-5b2d-4c8e-9f31-0a6d2e8b4001",
      kind: "terminal",
      mode: "view",
      title: "Share-mask timing bound",
      has_password: true,
      expires_at: NOW + 6 * 3600,
      created_at: NOW - 3600,
      revoked_at: null,
      revoked_reason: null,
      open_count: 3,
      last_opened_at: NOW - 600,
    },
    {
      share_id: "shr_fixture_0002",
      session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
      kind: "api",
      mode: "view",
      title: "API keys review fixes",
      has_password: false,
      expires_at: NOW + 2 * 86400,
      created_at: NOW - 7200,
      revoked_at: null,
      revoked_reason: null,
      open_count: 0,
      last_opened_at: null,
    },
  ],
};

/** A 2FA enrollment. FAKE: a made-up base32 key and recovery codes that open nothing. */
export const TWOFA_ENROLL = {
  secret: "JBSWY3DPEHPK3PXP",
  otpauth_uri: "otpauth://totp/BattleLab:commander?secret=JBSWY3DPEHPK3PXP&issuer=BattleLab",
  recovery_codes: ["7F3K-92QD", "LM4X-0PZ8", "QW7C-R2NB", "H8TY-55JK", "VN2E-XQ71", "ZD9P-6MAF"],
};
