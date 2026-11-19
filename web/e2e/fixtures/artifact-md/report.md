# Operating model — concept map

The **BattleLab operating model** in one page: who decides, who builds, and how a mission moves
from an idea to something an operator can rely on. See [the brief](brief.md) and
[the source](https://example.com/source).

## Roles

| Role | Decides | Builds | Reviews | Escalates to | Notes on the role, long enough to need room |
|---|---|---|---|---|---|
| Operator | scope, merge holds | — | every mission close | — | the one person who closes a mission |
| Supervisor | which agent, when | plans | the agent's gates | Operator | judges each gate with verbatim quotes |
| Agent | how | code, docs, artifacts | its own tests | Supervisor | never settles its own gate |

## Mission lifecycle

1. **Draft** — the operator states the goal and the done-criteria.
2. **Running** — agents work; the supervisor judges each gate.
3. **Review** — the operator reads the result and closes it.

- [x] Concept map agreed
- [ ] Escalation timings measured

> A gate is settled by an independent judge, never by the agent that did the work.

```yaml
mission:
  state: running
  gates: [plan, build, verify, a-very-long-gate-name-that-makes-this-line-wider-than-a-phone]
```

![Remote chart](https://tracker.example/chart.png)

<script>document.body.dataset.injected = "1"</script> <b>raw HTML</b> stays text.
