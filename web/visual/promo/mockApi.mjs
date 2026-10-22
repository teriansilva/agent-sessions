// ONE `/api/**` handler, dispatched by pathname (a catch-all registered beside specific routes
// shadows them — the last registered wins). Unknown paths answer `{}` and are reported.
import * as F from "./fixtures.mjs";
import * as X from "./extras.mjs";

const json = (body, status = 200) => ({ status, body });

export async function routeApi(page, state) {
  const { unhandled } = state;
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const q = url.searchParams;
    const method = req.method();
    let res;
    try {
      res = await answer(path, q, method, req, state);
    } catch (e) {
      console.log(`  [mock error] ${method} ${path}: ${e}`);
    }
    if (!res) {
      unhandled.add(`${method}:${path}`);
      res = json({});
    }
    if (res.raw) return route.fulfill(res.raw);
    return route.fulfill({ status: res.status, json: res.body });
  });
}

async function answer(path, q, method, req, state) {
  // ---- order matters: specific before prefix ------------------------------------------------
  if (/^\/api\/sessions\/[^/]+\/draft$/.test(path))
    return json({ id: decodeURIComponent(path.split("/")[3]), text: "", attachments: [], updated_at: null });

  switch (path) {
    case "/api/prefs":
      // The map persists its layout (expanded groups, window slots); accept and echo it.
      if (method === "POST") return json(req.postDataJSON() ?? {});
      return null;
    case "/api/config":
      return json({ ...F.CONFIG, theme: state.theme ?? "dark", ...(state.config ?? {}) });
    case "/api/version":
      return json({ version: "0.19.4" });
    case "/api/engines":
      return json({ engines: F.ROSTER.engines, problems: [] });
    case "/api/projects":
      return json(F.projectsList());
    case "/api/folders": {
      const folders = F.PROJECTS.map((p) => ({ cwd: p.folders[0], label: p.name }));
      // `scratchFolder` (a sweep state): one folder no project owns, which Settings offers to rename.
      if (state.scratchFolder) folders.push({ cwd: `${F.HOME}/scratch`, label: "scratch" });
      return json({ folders });
    }
    case "/api/pulse/notifications":
      return json({ notifications: [], unread: 0, uncertain: 0, settled: [] });
    case "/api/pulse/needs-you":
      return json(F.needsYou());
    case "/api/pulse/recap":
      return json(F.RECAP);
    case "/api/pulse":
      return json({ cards: [], generated_at: F.NOW - 60, window_days: 1 });
    case "/api/pulse/orchestrator":
      // A healthy, quiet orchestrator: suggest tier, nothing pending, the last pass succeeded.
      return json({
        config: {
          enabled: true,
          autonomy: "suggest",
          allowed_verbs: [],
          auto_verbs_ceiling: [],
          confidence_min: 0.8,
          interval_minutes: 5,
          max_actions_per_pass: 3,
          proposal_ttl_minutes: 60,
          stale_hours: 24,
          nudge_template: "",
          notify: "escalations",
          configured: true,
          default_nudge_template: "",
          auto_ai_directions: false,
          ai_direction_confidence_min: 0.95,
          ai_direction_confidence_floor: 0.9,
          ai_direction_confidence_max: 1,
        },
        pending: [],
        feed: [],
        expired_now: 0,
        delivering_verbs: [],
        last: { orchestrator: { finished_at: F.NOW - 120, ok: true, consecutive_failures: 0 } },
      });
    case "/api/dashboard/sessions":
      return json(F.DASH);
    case "/api/agents/usage":
      return json(F.USAGE);
    case "/api/missions":
      if (method === "GET") {
        // `archivedMission` (a sweep state): the archived scope holds one mission to bring back.
        if (state.archivedMission && q.get("archived") === "1") return json(X.archivedMissionsList());
        return json(F.missionsList(q));
      }
      break;
    case "/api/ask/unread":
      return json({ unread: false, conversations: 0 });
    case "/api/automations/origins":
      return json({ origins: {} });
    // The Library and Automations pages read their limits from these (#1531 sweep): a `{}` answer
    // crashed them into the error fallback. Valid, empty catalogs — the pages' own empty states.
    case "/api/automations":
      if (method === "GET")
        return json({
          // `disabledAutomation` (a sweep state): one automation that is off, so Enable… asks consent.
          automations: state.disabledAutomation ? [X.disabledAutomation()] : [],
          engines: [],
          loop: { enabled: true, owner: true },
          limits: {
            interval_min_minutes: 15,
            max_runs_per_day_max: 48,
            max_concurrent_max: 4,
            pause_after_failures_max: 10,
            triggers: ["schedule"],
            actions: ["prompt"],
            autonomy: ["ask", "auto"],
            timezone: "Europe/Bucharest",
          },
        });
      break;
    case "/api/library/skills":
      return json({ skills: [] });
    // The template library (#1531 sweep): the picker, the session chooser and their delete confirm.
    case "/api/templates":
      if (method === "GET") return json(X.TEMPLATES);
      break;
    case "/api/template-variables":
      if (method === "GET") return json({ variables: [], limits: { secret_min: 8 } });
      break;
    case "/api/fs/dirs":
      return json(X.fsDirs(q));
    case "/api/shares":
      if (method === "GET") return json({ available: true, reason: null, shares: [] });
      break;
    case "/api/memory":
      if (method === "GET") return json({ projects: [] });
      break;
    case "/api/variables":
      if (method === "GET")
        return json({
          variables: [],
          collections: [],
          migration_required: false,
          limits: { variables_max: 200, value_max: 4096, name_max: 64, secret_min: 8, collections_max: 20 },
        });
      break;
    case "/api/ask/conversations":
      state.ask ??= [];
      if (method === "GET")
        return json({ conversations: state.ask.map(X.askSummary), configured: true });
      if (method === "POST") {
        const c = { id: "ask_" + "c0ffee".padEnd(32, "0"), created_at: F.NOW - 30, updated_at: F.NOW - 30, turns: [] };
        state.ask.push(c);
        return json(X.askBody(c), 201);
      }
      break;
    case "/api/sessions": {
      if (method !== "GET") break;
      let rows = F.SESSIONS;
      const running = q.get("running");
      if (running === "live") rows = rows.filter((r) => r.running);
      if (running === "working") rows = rows.filter((r) => r.working);
      const project = q.get("project");
      if (project) rows = rows.filter((r) => r.project.id === project);
      const engine = q.get("engine");
      if (engine) rows = rows.filter((r) => r.engine === engine);
      if (q.get("archived") === "1") rows = [];
      return json(F.sessionsPage(rows));
    }
  }

  const stream = /^\/api\/ask\/conversations\/(ask_[0-9a-f]{32})\/message\/stream$/.exec(path);
  if (stream) return X.askStream(stream[1], req, state);

  const askConv = /^\/api\/ask\/conversations\/(ask_[0-9a-f]{32})$/.exec(path);
  if (askConv) {
    const c = (state.ask ?? []).find((x) => x.id === askConv[1]);
    return c ? json(X.askBody(c)) : json({ detail: "no such conversation" }, 404);
  }

  const one = /^\/api\/sessions\/([^/]+)$/.exec(path);
  if (one && method === "GET") {
    const id = decodeURIComponent(one[1]);
    const row = F.SESSIONS.find((s) => s.id === id) ?? X.extraSession(id);
    return row ? json(row) : json({ detail: "not found" }, 404);
  }

  return X.answerExtra?.(path, q, method, req, state);
}
