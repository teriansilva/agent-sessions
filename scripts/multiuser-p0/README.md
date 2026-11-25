# Multi-user P0 harness (#2370)

Lab tooling for the multi-user epic's P0 (#1469). It measures confinement candidates for API
sessions against the attacker matrix and the performance budgets in
[`docs/multi-user/contract.md`](../../docs/multi-user/contract.md) §7–§9. Nothing in the app
imports it.

| File | What it is |
|---|---|
| `harness` | The CLI: `preflight`, `host-facts`, `check FILE.json`, `summary FILE.json` |
| `mu_p0.py` | The library: preflight refusal, candidate A (bubblewrap) and B (systemd unit) envelopes, verdict rules, benchmark statistics, evidence schema |
| `landlock_exec.py` | Applies a Landlock ruleset to itself, then execs a command. It fails closed (exit 111) |
| `probe.py` | Takes the vendor CLI's place inside an envelope and reports what it could reach and what it inherited |

**Where it runs.** Only inside the disposable VM the operator approved for P0b. `preflight`
refuses when any of these holds:
- the marker file `/etc/battlelab-multiuser-p0` is absent (it lists the two test accounts);
- it runs as root;
- it runs as any other account, or as an account in an administrator-equivalent group;
- the host is listed in `$MULTIUSER_P0_REFUSE_HOSTS`.

It never creates or changes accounts, and it uses synthetic credentials only.

**Verdicts** are `pass`, `fail`, `unsupported` or `not-run`, and they never merge. A
`connection refused` is a **fail** for a reachability row: it proves the path to the service
exists. A `not found` (ENOENT) counts as a denial only when the harness first verified,
outside the envelope, that the target is really reachable (`mu_p0.prepare_plan` marks each
filesystem row; a dangling link or a loop is not reachable), and the probe carries the mark into
its row; otherwise the row is `not-run`. P0b's runner calls it before each envelope starts. `harness check`
refuses evidence whose values are empty, blank or placeholders, whose declared probe IDs lack
rows, whose samples are missing, non-finite or lack the launch, first-byte and throughput arms, whose SHA is not a commit in the
checkout, or whose sample arms are below the protocol's counts (exit 1); it exits 4 on a file
it cannot read.

The pure logic is pinned by `tests/test_multiuser_p0_harness.py`. That includes one real Landlock
enforcement test in a child process, which is skipped on kernels without Landlock.
