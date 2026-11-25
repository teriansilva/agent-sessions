"""The multi-user P0 harness's own logic (#2370; `docs/multi-user/contract.md` §7–§9).

The harness measures confinement only inside the operator-approved VM, so what runs here is its
logic: the preflight refusal, the candidate envelopes, the verdict rules, the benchmark
statistics, the evidence schema and the probe. The one real enforcement test (Landlock in a child
process, which changes nothing outside that child) skips on a kernel without Landlock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "scripts" / "multiuser-p0"
sys.path.insert(0, str(HERE))

import landlock_exec  # noqa: E402
import mu_p0  # noqa: E402
import probe  # noqa: E402

GOOD_MARKER = "mu-alice\nmu-bob\n"


def _pre(**kw):
    args = dict(
        marker=GOOD_MARKER,
        hostname="p0-vm",
        refuse_hosts="",
        user="mu-alice",
        uid=1001,
        groups={"mu-alice"},
    )
    args.update(kw)
    return mu_p0.preflight(**args)


# ---- preflight --------------------------------------------------------------------------------


def test_preflight_passes_only_on_the_marked_vm_as_a_test_account():
    assert _pre() == []


@pytest.mark.parametrize(
    "kw, needle",
    [
        ({"marker": None}, "not the approved disposable VM"),
        ({"marker": "mu-alice\n"}, "two test accounts"),
        ({"refuse_hosts": "prod-a, P0-VM"}, "refusal list"),
        ({"uid": 0}, "root"),
        ({"user": "operator"}, "not one of the VM's test accounts"),
        ({"groups": {"mu-alice", "docker"}}, "privileged groups"),
        ({"groups": {"sudo"}}, "privileged groups"),
    ],
)
def test_preflight_refuses(kw, needle):
    problems = _pre(**kw)
    assert any(needle in p for p in problems), problems


# ---- candidate A ------------------------------------------------------------------------------


def _a(**kw):
    args = dict(
        workspace="/home/mu-alice/projects/demo",
        home="/home/mu-alice",
        agent_home="/var/lib/p0/accounts/claude/a1",
        state_dir="/var/lib/p0/state/s1",
        sockets=["/run/p0/broker.sock"],
        root_links={"/bin": "usr/bin", "/lib": "usr/lib"},
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        landlock=[
            "/usr/bin/python3",
            "-I",
            "/opt/ll.py",
            "--rw",
            "/home/mu-alice/projects/demo",
            "--",
        ],
        command=["/usr/bin/probe", "/plan.json"],
        unshare_net=True,
    )
    args.update(kw)
    return mu_p0.candidate_a_argv(**args)


def test_candidate_a_isolates_namespaces_and_hides_the_real_home():
    argv = _a()
    assert argv[0] == "/usr/bin/bwrap"
    for flag in (
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--unshare-net",
    ):
        assert flag in argv
    # HOME is a tmpfs under /home with only the one agent account bound at it.
    assert argv[argv.index("--tmpfs", argv.index("/tmp")) + 1] == "/home"
    i = argv.index("/var/lib/p0/accounts/claude/a1")
    assert argv[i - 1] == "--bind" and argv[i + 1] == "/home/mu-alice"
    # The workspace and the state dir are bound read-write; nothing else under /home is.
    triples = [argv[k : k + 3] for k in range(len(argv) - 2) if argv[k] == "--bind"]
    ws = "/home/mu-alice/projects/demo"
    assert ["--bind", ws, ws] in triples
    assert [t[1] for t in triples] == [
        "/var/lib/p0/accounts/claude/a1",
        "/var/lib/p0/state/s1",
        ws,
        "/run/p0/broker.sock",
    ]
    # The command runs after Landlock.
    sep = argv.index("--", argv.index("--clearenv"))
    assert argv[sep + 1 : sep + 3] == ["/usr/bin/python3", "-I"]
    assert argv[-2:] == ["/usr/bin/probe", "/plan.json"]


def test_candidate_a_never_passes_the_user_manager_to_the_child():
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        with pytest.raises(mu_p0.Refused, match="user manager"):
            _a(env={"PATH": "/usr/bin", key: "/run/user/1001"})


@pytest.mark.parametrize(
    "kw",
    [
        {"workspace": "relative/demo"},
        {"workspace": "/"},
        {"workspace": "/home/mu-alice/../mu-bob"},
        {"sockets": ["run/x.sock"]},
        {"command": ["probe"]},
        {"env": {"bad-key": "x"}},
    ],
)
def test_candidate_a_refuses_bad_inputs(kw):
    with pytest.raises(mu_p0.Refused):
        _a(**kw)


def test_candidate_a_shares_the_network_only_when_asked():
    assert "--unshare-net" not in _a(unshare_net=False)


# ---- candidate B ------------------------------------------------------------------------------


def test_candidate_b_sandboxes_the_whole_unit():
    props = mu_p0.candidate_b_properties(
        workspace="/home/mu-alice/projects/demo",
        state_dir="/var/lib/p0/state/s1",
        app_socket="/run/p0/app.sock",
    )
    assert all(p.startswith("--property=") for p in props)
    joined = " ".join(props)
    for want in (
        "PrivateUsers=yes",
        "ProtectHome=tmpfs",
        "ProtectSystem=strict",
        "BindPaths=/home/mu-alice/projects/demo /var/lib/p0/state/s1",
        "BindReadOnlyPaths=/run/p0/app.sock",
        "RestrictNamespaces=yes",
    ):
        assert want in joined


# ---- verdicts ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected, observed, detail, supported, want",
    [
        ("denied", "denied", "EACCES", True, "pass"),
        ("denied", "denied", "create EPERM", True, "pass"),
        ("denied", "denied", "timeout", True, "pass"),
        ("denied", "denied", "ECONNREFUSED", True, "fail"),  # the service was reachable
        ("denied", "denied", "EIO", True, "fail"),  # not a denial: never counted as one
        ("denied", "allowed", "", True, "fail"),
        ("allowed", "allowed", "", True, "pass"),
        ("allowed", "denied", "EACCES", True, "fail"),
        ("denied", None, "", True, "not-run"),
        ("denied", "denied", "EACCES", False, "unsupported"),
    ],
)
def test_verdict(expected, observed, detail, supported, want):
    assert (
        mu_p0.verdict(expected=expected, observed=observed, detail=detail, supported=supported)
        == want
    )


def test_not_there_counts_only_for_a_target_verified_outside_the_envelope():
    # A hidden path is the intended denial only if the target really exists outside the sandbox;
    # a canary that was never created proves nothing (Hermes, review 8923).
    assert mu_p0.verdict(expected="denied", observed="denied", detail="ENOENT") == "not-run"
    assert (
        mu_p0.verdict(expected="denied", observed="denied", detail="ENOENT", target_exists=True)
        == "pass"
    )


def test_summary_keeps_the_four_verdicts_apart():
    rows = [{"id": "A1", "verdict": v} for v in ("pass", "pass", "not-run", "unsupported")]
    assert mu_p0.summarize(rows) == {"A1": {"pass": 2, "fail": 0, "unsupported": 1, "not-run": 1}}


# ---- statistics -------------------------------------------------------------------------------


def test_percentile_is_nearest_rank():
    samples = list(range(1, 101))
    assert mu_p0.percentile(samples, 95) == 95
    assert mu_p0.percentile([5.0], 95) == 5.0
    assert mu_p0.percentile([1, 2, 3, 4], 50) == 2


def test_launch_budget_is_the_p95_of_paired_differences():
    base = [100.0] * 20
    ok, p95 = mu_p0.launch_within_budget(base, [350.0] * 19 + [900.0])
    assert ok and p95 == 250.0  # one outlier above p95 does not decide it
    ok, p95 = mu_p0.launch_within_budget(base, [450.0] * 20)
    assert not ok and p95 == 350.0
    with pytest.raises(mu_p0.Refused):
        mu_p0.launch_within_budget(base, base[:5])


def test_throughput_rule_tolerates_noise_and_catches_a_loss():
    base = [100.0 + (i % 7) for i in range(200)]
    noisy = [99.0 + (i % 9) for i in range(200)]
    ok, info = mu_p0.throughput_holds(base, noisy)
    assert ok, info
    slow = [80.0 + (i % 7) for i in range(200)]
    ok, info = mu_p0.throughput_holds(base, slow)
    assert not ok and info["median_ratio"] < 0.9
    assert mu_p0.throughput_holds(base, noisy) == mu_p0.throughput_holds(base, noisy)  # seeded


# ---- evidence ---------------------------------------------------------------------------------


def _evidence():
    return {
        "meta": {
            "source_sha": "a" * 40,
            "host": {"kernel": "6.8.0", "landlock_abi": 4, "bwrap": "0.9.0", "systemd": "255"},
            "backend": "A",
            "policy": {"ro": ["/usr"]},
            "probe_ids": ["A1", "A2"],
            "approval": "operator comment link",
        },
        "rows": [
            {"id": "A1", "verdict": "pass", "evidence": "EACCES on /home/mu-bob/canary"},
            {"id": "A2", "verdict": "not-run"},
        ],
        "samples": {
            "launch_ms_A": [120.0 + i for i in range(20)],
            "first_byte_ms_A": [30.0 + (i % 7) for i in range(200)],
            "throughput_mbps_A": [99.0 + (i % 5) for i in range(200)],
        },
    }


def test_complete_evidence_has_no_problems():
    assert mu_p0.evidence_problems(_evidence()) == []


def test_a_backend_that_could_not_run_needs_no_samples():
    # Every row unsupported: the backend never ran here, so there is nothing to measure; that is
    # a valid, complete record of "unsupported" (contract §8 exit rule), not missing proof.
    e = _evidence()
    e["rows"] = [{"id": "A1", "verdict": "unsupported"}, {"id": "A2", "verdict": "unsupported"}]
    e.pop("samples")
    assert mu_p0.evidence_problems(e) == []


def test_prepare_plan_checks_targets_on_the_host_and_the_probe_carries_it(tmp_path):
    real = tmp_path / "canary"
    real.write_text("x")
    plan = {
        "read": [
            {"id": "A4", "path": str(real)},
            {"id": "A4", "path": str(tmp_path / "never-created")},
        ],
        "tcp": [{"id": "A5", "host": "127.0.0.1", "port": 9}],
    }
    prepared = mu_p0.prepare_plan(plan)
    assert [i["target_exists"] for i in prepared["read"]] == [True, False]
    assert "target_exists" not in prepared["tcp"][0]
    assert "target_exists" not in plan["read"][0]  # the input plan is not mutated
    # Inside an envelope both would be ENOENT; only the verified one may count as a denial.
    rows = [
        probe._carry({"id": "A4", "observed": "denied", "detail": "ENOENT"}, item)
        for item in prepared["read"]
    ]
    assert [mu_p0.judge(r, expected="denied")["verdict"] for r in rows] == ["pass", "not-run"]


def _judged_enoent(item: dict) -> str:
    """What a target's ENOENT from inside an envelope would be judged, given the host check."""
    row = probe._carry({"id": "A4", "observed": "denied", "detail": "ENOENT"}, item)
    return mu_p0.judge(row, expected="denied")["verdict"]


def test_only_a_reachable_target_lets_enoent_count(tmp_path):
    # Review 8928: presence is not proof. A dangling link "exists" to lexists, a loop resolves to
    # nothing, and an unreadable file cannot be shown to be readable outside the envelope.
    readable = tmp_path / "canary"
    readable.write_text("x")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "gone")
    loop_a, loop_b = tmp_path / "loop-a", tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    locked = tmp_path / "locked"
    locked.write_text("x")
    locked.chmod(0)
    if os.access(locked, os.R_OK):
        pytest.skip("running with privileges that bypass file modes")
    link_to_canary = tmp_path / "link-to-canary"
    link_to_canary.symlink_to(readable)
    plan = {
        "read": [
            {"id": "A4", "path": str(p)}
            for p in (readable, dangling, loop_a, locked, link_to_canary)
        ]
    }
    got = [_judged_enoent(item) for item in mu_p0.prepare_plan(plan)["read"]]
    assert got == ["pass", "not-run", "not-run", "not-run", "pass"]
    locked.chmod(0o600)


def test_a_write_target_is_reachable_as_a_writable_file_or_under_a_writable_dir(tmp_path):
    existing = tmp_path / "canary"
    existing.write_text("x")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "gone" / "deeper")
    plan = {
        "write": [
            {"id": "A1", "path": str(existing)},
            {"id": "A1", "path": str(tmp_path / "new-file")},  # a writable parent directory
            {"id": "A1", "path": str(tmp_path / "no-such-dir" / "f")},
            {"id": "A1", "path": str(dangling)},
        ]
    }
    marks = [i["target_exists"] for i in mu_p0.prepare_plan(plan)["write"]]
    assert marks == [True, True, False, False]


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda e: e["meta"].pop("approval"), "meta.approval"),
        (lambda e: e["meta"].update(source_sha="abc123"), "full 40-hex"),
        (lambda e: e["meta"]["host"].pop("landlock_abi"), "landlock_abi"),
        (lambda e: e["rows"][0].pop("evidence"), "pass without evidence"),
        (lambda e: e["rows"][1].update(verdict="skipped"), "verdict must be"),
        (lambda e: e["rows"].append({"id": "A9", "verdict": "pass", "evidence": "x"}), "not in"),
        (lambda e: e["samples"].update(launch_ms_A="p95=120"), "raw numbers"),
        # Values, not only keys (Hermes, review 8923).
        (lambda e: e["meta"].update(approval=""), "meta.approval is empty"),
        (lambda e: e["meta"].update(backend=""), "meta.backend is empty"),
        (lambda e: e["meta"].update(policy={}), "meta.policy is empty"),
        (lambda e: e["meta"].update(probe_ids=[]), "meta.probe_ids is empty"),
        (lambda e: e["meta"]["host"].update(bwrap=None), "meta.host.bwrap is empty"),
        (lambda e: e["meta"]["host"].update(kernel=" "), "meta.host.kernel is empty"),
        (lambda e: e["meta"].update(source_sha="0" * 40), "all-zero placeholder"),
        (lambda e: e["samples"].update(launch_ms_A=[1.0]), "needs at least 20"),
        (lambda e: e["samples"].update(throughput_mbps_A=[1.0] * 199), "needs at least 200"),
        # Round two (Hermes, review 8926): absent proof, blank-but-present values, non-finite.
        (lambda e: e.pop("samples"), "samples is missing or empty"),
        (lambda e: e.update(samples={}), "samples is missing or empty"),
        (lambda e: e["samples"].pop("first_byte_ms_A"), "no first_byte_* arm"),
        (lambda e: e["samples"].pop("launch_ms_A"), "no launch_* arm"),
        (lambda e: e.update(rows=[]), "A1 is declared in meta.probe_ids but has no row"),
        (lambda e: e["rows"].pop(1), "A2 is declared in meta.probe_ids but has no row"),
        (lambda e: e["meta"].update(approval="   "), "meta.approval is empty"),
        (lambda e: e["meta"].update(backend="\t"), "meta.backend is empty"),
        (lambda e: e["samples"].update(launch_ms_A=[float("nan")] * 20), "finite"),
        (lambda e: e["samples"].update(throughput_mbps_A=[float("inf")] * 200), "finite"),
        (lambda e: e.update(rows={"A1": {"verdict": "pass"}}), "rows must be a list of objects"),
        (lambda e: e.update(rows=["A1"]), "rows must be a list of objects"),
        (lambda e: e["samples"].update(misc=[1.0] * 500), "not a protocol sample family"),
        (lambda e: e["samples"].update(launch_ms_A=[True] * 20), "raw numbers"),
    ],
)
def test_incomplete_evidence_is_refused(mutate, needle):
    e = _evidence()
    mutate(e)
    assert any(needle in p for p in mu_p0.evidence_problems(e))


# ---- the probe --------------------------------------------------------------------------------


def test_probe_reports_allowed_and_denied(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("x")
    locked = tmp_path / "locked.txt"
    locked.write_text("secret")
    locked.chmod(0)
    if os.access(locked, os.R_OK):
        pytest.skip("running with privileges that bypass file modes")
    rows = probe.run(
        {
            "read": [{"id": "A1", "path": str(ok)}, {"id": "A1", "path": str(locked)}],
            "symlink": [{"id": "A1", "link": str(tmp_path / "l"), "target": str(locked)}],
            "unix": [{"id": "A3", "path": str(tmp_path / "absent.sock")}],
        }
    )
    got = [(r["probe"], r["observed"], r["detail"]) for r in rows]
    assert got[0] == ("read", "allowed", "")
    assert got[1] == ("read", "denied", "EACCES")
    assert got[2] == ("symlink", "denied", "EACCES")
    assert got[3][:2] == ("unix", "denied")
    locked.chmod(0o600)


@pytest.mark.parametrize("port", [0, 65536, True, "443", 4.0])
def test_probe_refuses_bad_ports(port):
    with pytest.raises(ValueError):
        probe.try_tcp("A5", "127.0.0.1", port)


@pytest.mark.parametrize("pid", [0, 1, -1, True, "12"])
def test_probe_refuses_pid_0_1_and_groups(pid):
    with pytest.raises(ValueError):
        probe.try_signal("A2", pid)


def test_probe_reports_what_it_inherited():
    info = probe.inherited()
    assert set(info) == {"fds", "env_keys", "namespaces"}
    assert "pid" in info["namespaces"]


# ---- Landlock ---------------------------------------------------------------------------------


def test_landlock_parse_refuses_relative_paths_and_bad_ports():
    for argv in (
        ["--ro", "rel", "--", "/usr/bin/true"],
        ["--connect", "0", "--", "/bin/true"],
        ["--ro", "/usr", "/usr/bin/true"],
        ["--ro", "/usr", "--", "true"],
    ):
        with pytest.raises(landlock_exec.Refused):
            landlock_exec.parse(argv)


def test_landlock_handled_rights_follow_the_abi():
    assert landlock_exec.handled_fs(1) == (1 << 13) - 1
    assert landlock_exec.handled_fs(3) & landlock_exec.FS["truncate"]
    assert not landlock_exec.handled_fs(4) & landlock_exec.FS["ioctl_dev"]
    assert landlock_exec.handled_fs(5) & landlock_exec.FS["ioctl_dev"]


@pytest.mark.skipif(landlock_exec.abi() < 1, reason="no Landlock on this kernel")
def test_landlock_denies_what_the_ruleset_does_not_grant(tmp_path):
    inside, outside = tmp_path / "in", tmp_path / "out"
    inside.mkdir()
    outside.mkdir()
    (inside / "a").write_text("ok")
    (outside / "b").write_text("secret")
    script = Path(landlock_exec.__file__)
    # /dev/urandom: some interpreters read it at startup, so a real policy grants it too.
    ro = [
        "/usr",
        "/etc",
        "/lib",
        "/lib64",
        "/dev/urandom",
        sys.base_prefix,
        sys.prefix,
        str(script),
        str(inside),
    ]
    args = [a for p in dict.fromkeys(ro) if os.path.exists(p) for a in ("--ro", p)]
    child = (
        "import sys\n"
        f"print(open({str(inside / 'a')!r}).read())\n"
        f"try:\n    open({str(outside / 'b')!r}).read(); print('LEAK')\n"
        "except PermissionError: print('denied')\n"
        f"try:\n    open({str(inside / 'new')!r}, 'w'); print('WROTE')\n"
        "except PermissionError: print('write-denied')\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", str(script), *args, "--", sys.executable, "-I", "-c", child],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.stdout.split() == ["ok", "denied", "write-denied"], out.stderr


@pytest.mark.skipif(landlock_exec.abi() < 1, reason="no Landlock on this kernel")
def test_landlock_exec_fails_closed_when_the_command_is_not_allowed():
    script = Path(landlock_exec.__file__)
    out = subprocess.run(
        [sys.executable, "-I", str(script), "--ro", str(script), "--", "/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == landlock_exec.REFUSED, out.stderr
    assert "refused" in out.stderr


def test_harness_cli_refuses_off_the_vm(tmp_path):
    out = subprocess.run(
        [sys.executable, "-I", str(HERE / "harness"), "preflight"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if mu_p0.MARKER.exists():  # pragma: no cover - only true inside the approved VM
        pytest.skip("running inside the marked VM")
    assert out.returncode == 3 and "not the approved disposable VM" in out.stderr


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-I", str(HERE / "harness"), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_harness_cli_checks_evidence(tmp_path):
    head = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        pytest.skip("not running from a git checkout")
    path = tmp_path / "e.json"
    e = _evidence()
    e["meta"]["source_sha"] = head
    path.write_text(json.dumps(e))
    assert _cli("check", str(path)).returncode == 0
    summary = json.loads(_cli("summary", str(path)).stdout)
    assert summary["A1"]["pass"] == 1 and summary["A2"]["not-run"] == 1
    # A well-formed SHA that is not a commit here is refused.
    e["meta"]["source_sha"] = "ab" * 20
    path.write_text(json.dumps(e))
    out = _cli("check", str(path))
    assert out.returncode == 1 and "not a commit" in out.stdout


def test_harness_cli_exits_4_when_it_cannot_read_the_checkout(tmp_path):
    # A copy of the harness outside any git checkout cannot verify the SHA: exit 4, no traceback.
    lab = tmp_path / "x" / "scripts" / "multiuser-p0"
    lab.mkdir(parents=True)
    for name in ("harness", "mu_p0.py", "landlock_exec.py", "probe.py"):
        (lab / name).write_bytes((HERE / name).read_bytes())
    path = tmp_path / "e.json"
    path.write_text(json.dumps(_evidence()))
    out = subprocess.run(
        [sys.executable, "-I", str(lab / "harness"), "check", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 4 and "cannot read the checkout" in out.stderr, out.stderr
    assert "Traceback" not in out.stderr


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_harness_cli_refuses_unreadable_evidence(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    out = _cli("check", str(path))
    assert out.returncode == 4 and "unreadable evidence" in out.stderr
    assert _cli("check", str(tmp_path / "missing.json")).returncode == 4
