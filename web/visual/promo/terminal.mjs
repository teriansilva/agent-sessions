// The terminal socket: string frames are control JSON, binary frames are PTY bytes (termSocket.ts).
// Each session key gets its own plausible, invented agent screen.
const E = "\x1b[";
const c = (code, s) => `${E}${code}m${s}${E}0m`;
const dim = (s) => c("2", s);
const bold = (s) => c("1", s);
const grn = (s) => c("32", s);
const red = (s) => c("31", s);
const ylw = (s) => c("33", s);
const cyn = (s) => c("36", s);
const mag = (s) => c("35", s);
const gry = (s) => c("90", s);
const amber = (s) => c("38;5;214", s);

export const SCREENS = {
  claude: [
    `${amber("✻")} ${bold("Welcome to Claude Code!")}`,
    gry("  cwd: /home/commander/code/orbital-api"),
    "",
    `${gry(">")} Add rate limiting to the public ingest API. Token bucket per API key,`,
    `  429 with Retry-After, and a load test that proves it holds at 2k rps.`,
    "",
    `${amber("●")} I'll add a token-bucket limiter keyed by API key, wire it into the ingest`,
    `  router, and verify it under load.`,
    "",
    `${grn("●")} ${bold("Read")}(src/ingest/router.ts)`,
    `  ${gry("⎿")}  Read ${bold("142")} lines`,
    "",
    `${grn("●")} ${bold("Write")}(src/ingest/limiter.ts)`,
    `  ${gry("⎿")}  Wrote ${bold("68")} lines to src/ingest/limiter.ts`,
    "",
    `${grn("●")} ${bold("Update")}(src/ingest/router.ts)`,
    `  ${gry("⎿")}  Updated src/ingest/router.ts with ${bold("9")} additions and ${bold("2")} removals`,
    `      ${gry("31")}   import { validate } from "./schema";`,
    `      ${gry("32")} ${c("48;5;22", '+ import { limiter } from "./limiter";')}`,
    `      ${gry("33")}  `,
    `      ${gry("34")} ${c("48;5;52", "- router.post(\"/v1/ingest\", validate, ingest);")}`,
    `      ${gry("35")} ${c("48;5;22", "+ router.post(\"/v1/ingest\", limiter({ rate: 50, burst: 100 }),")}`,
    `      ${gry("36")} ${c("48;5;22", "+   validate, ingest);")}`,
    "",
    `${grn("●")} ${bold("Bash")}(npm test -- limiter)`,
    `  ${gry("⎿")}  ${grn("PASS")} src/ingest/limiter.test.ts`,
    `       ${grn("✓")} refills at the configured rate (4 ms)`,
    `       ${grn("✓")} answers 429 with Retry-After when empty (2 ms)`,
    `       ${grn("✓")} keys buckets by API key, not by IP (1 ms)`,
    `     Tests: ${grn("12 passed")}, 12 total`,
    "",
    `${grn("●")} ${bold("Bash")}(k6 run load/ingest.js --vus 200 --duration 60s)`,
    `  ${gry("⎿")}  http_reqs......: ${bold("121,904")}  ${cyn("2,031.7/s")}`,
    `     http_req_duration: avg=${grn("18.2ms")} p(95)=${grn("41.7ms")}`,
    `     status 429.....: ${ylw("3.1%")} ${gry("(over-quota keys only)")}`,
    "",
    `${amber("●")} Holding at 2k rps with p95 under 42 ms. Only the two keys driven above`,
    `  quota see 429s, each with a correct ${bold("Retry-After")}. Writing the summary to`,
    `  docs/ingest-limits.md next.`,
    "",
    `${amber("✽")} ${amber("Documenting…")} ${gry("(38s · ↓ 2.1k tokens · esc to interrupt)")}`,
    "",
    "@@BOX@@",
    `  ${gry("⏵⏵ accept edits on · 1 mission objective open")}`,
  ],
  codex: [
    `${bold(">_ OpenAI Codex")} ${gry("(research preview)")}`,
    gry("  directory: ~/code/orbital-api"),
    "",
    `${cyn("user")}`,
    "Migrate the billing webhooks to the v2 event schema.",
    "",
    `${mag("codex")}`,
    "Planned the migration in three steps:",
    `  ${grn("✓")} add v2 columns and backfill from the payload JSON`,
    `  ${grn("✓")} dual-write in the webhook handler behind BILLING_V2`,
    `  ${ylw("□")} run the backfill on staging and compare row counts`,
    "",
    `${gry("$")} npm run db:migrate -- --dry-run`,
    `  ${grn("✔")} 20261021_billing_v2.sql  ${gry("(4 statements, est. 1.2s)")}`,
    "",
    `${ylw("▲ Allow command?")}`,
    `  npm run db:migrate -- --env staging`,
    "",
    `  ${bold("› 1. Yes")}`,
    "    2. Yes, and don't ask again for this command",
    "    3. No, tell Codex what to do differently",
  ],
  opencode: [
    `${cyn("opencode")} ${gry("v1.4 · harbor-web")}`,
    "",
    `${gry("┃")} Swap the hard-coded colours in checkout for design tokens`,
    "",
    `${cyn("◆")} Searching for hex colours in src/checkout…  ${gry("41 matches")}`,
    `${cyn("◆")} Edit src/checkout/Summary.tsx  ${grn("+12")} ${red("-12")}`,
    `${cyn("◆")} Edit src/checkout/Payment.tsx  ${grn("+9")} ${red("-9")}`,
    `${cyn("◆")} Edit src/checkout/Drawer.css   ${grn("+20")} ${red("-20")}`,
    `${cyn("◆")} Run  npm run test:visual -- checkout`,
    `   ${gry("…")} regenerating 18 snapshots (dark + light)`,
  ],
  gemini: [
    `${c("34", "✦ Gemini CLI")} ${gry("harbor-web")}`,
    "",
    `${gry(">")} The cart drawer e2e is flaky — find out why.`,
    "",
    `${c("34", "✦")} The focus trap moves focus while the drawer is still transitioning,`,
    "  so the first Tab lands on the page behind it about 1 run in 12.",
    "",
    `  Two fixes pass 200/200 locally:`,
    `  ${bold("1.")} wait for ${bold("transitionend")} before moving focus`,
    `  ${bold("2.")} move focus in a ${bold("requestAnimationFrame")}`,
    "",
    `${ylw("?")} Which one should I keep?`,
  ],
  kimi: [
    `${c("38;5;205", "Kimi Code")} ${gry("atlas-cli")}`,
    "",
    `${gry(">")} generate fish completions from the clap spec`,
    "",
    `${c("38;5;205", "•")} cargo run --bin gen-completions -- fish`,
    `  ${grn("wrote")} completions/atlas.fish ${gry("(312 lines)")}`,
    `${c("38;5;205", "•")} fish -N -c 'source completions/atlas.fish; complete -C "atlas dep"'`,
    "  deploy\tDeploy a stack",
    "  depends\tShow the dependency graph",
  ],
};

// Narrow screens (a phone is ~40 columns): the agent wraps to the width it is given.
export const NARROW = {
  claude: [
    `${amber("✻")} ${bold("Claude Code")} ${gry("· orbital-api")}`,
    "",
    `${gry(">")} Rate-limit the public ingest API.`,
    `  Token bucket per key, 429 with`,
    `  Retry-After, prove 2k rps.`,
    "",
    `${grn("●")} ${bold("Write")}(src/ingest/limiter.ts)`,
    `  ${gry("⎿")}  Wrote ${bold("68")} lines`,
    "",
    `${grn("●")} ${bold("Update")}(src/ingest/router.ts)`,
    `  ${gry("⎿")}  ${bold("9")} additions, ${bold("2")} removals`,
    `  ${c("48;5;52", "- router.post(url, validate,")}`,
    `  ${c("48;5;22", "+ router.post(url, limiter({")}`,
    `  ${c("48;5;22", "+   rate: 50, burst: 100 }),")}`,
    "",
    `${grn("●")} ${bold("Bash")}(npm test -- limiter)`,
    `  ${gry("⎿")}  ${grn("PASS")} limiter.test.ts`,
    `     ${grn("✓")} refills at the set rate`,
    `     ${grn("✓")} 429 with Retry-After`,
    `     ${grn("✓")} keyed by API key, not IP`,
    `     Tests: ${grn("12 passed")}`,
    "",
    `${grn("●")} ${bold("Bash")}(k6 run load/ingest.js)`,
    `  ${gry("⎿")}  ${cyn("2,031.7 req/s")}`,
    `     p(95)=${grn("41.7ms")}  429s: ${ylw("3.1%")}`,
    "",
    `${amber("●")} Holding at 2k rps, p95 under`,
    `  42 ms. Only over-quota keys`,
    `  see 429s. Writing the docs next.`,
    "",
    `${amber("✽")} ${amber("Documenting…")} ${gry("(38s · esc)")}`,
    "",
    "@@BOX@@",
    `  ${gry("⏵⏵ accept edits on")}`,
  ],
};

function screenFor(key, cols) {
  const engine = key.split(":")[0];
  if (cols && cols < 70 && NARROW[engine]) {
    const w = cols - 1;
    return NARROW[engine].flatMap((l) =>
      l === "@@BOX@@"
        ? [gry("╭" + "─".repeat(w - 2) + "╮"), `${gry("│")} ${gry(">")} ${" ".repeat(w - 5)}${gry("│")}`, gry("╰" + "─".repeat(w - 2) + "╯")]
        : [l],
    );
  }
  const w = Math.max(40, Math.min(cols || 100, 160)) - 2;
  return (SCREENS[engine] ?? SCREENS.claude).flatMap((l) =>
    l === "@@BOX@@"
      ? [
          gry("╭" + "─".repeat(w - 2) + "╮"),
          `${gry("│")} ${gry(">")} ${" ".repeat(w - 5)}${gry("│")}`,
          gry("╰" + "─".repeat(w - 2) + "╯"),
        ]
      : [l],
  );
}

/** `opts` (the NIGHTJAR sweep's `term`, #1568) puts the socket in a state the promo never needs:
 *  `silent` — never answers, so the pane stays "connecting"; `closeCode` — closes with that code
 *  (4403 is a refusal the pane shows as rejected; 4409 is busy, which it retries);
 *  `role` / `holder` — the viewer is a read-only secondary behind the take-over banner. */
export async function routeTerminal(page, opts = {}) {
  await page.routeWebSocket(/\/ws\//, (ws) => {
    const url = new URL(ws.url());
    const m = /\/ws\/(?:term\/)?([^/?]+)/.exec(url.pathname);
    const key = m ? decodeURIComponent(m[1]) : "claude:x";
    ws.onMessage(() => {});
    if (opts.silent) return;
    if (opts.closeCode) {
      setTimeout(() => ws.close({ code: opts.closeCode, reason: opts.closeReason ?? "" }), 150);
      return;
    }
    setTimeout(() => {
      ws.send(JSON.stringify({ t: "role", role: opts.role ?? "owner", ...(opts.holder ? { holder: opts.holder } : {}) }));
      const text = "\x1b[2J\x1b[H" + screenFor(key, Number(url.searchParams.get("cols"))).join("\r\n");
      const buf = Buffer.from(text);
      ws.send(buf);
      ws.send(JSON.stringify({ t: "seq", n: buf.length }));
    }, 150);
  });
}
