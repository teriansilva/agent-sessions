"""The multi-user P0 harness library (#2370; the rules are `docs/multi-user/contract.md` §7–§9).

Pure functions where it matters, so the unit tests in `tests/test_multiuser_p0_harness.py` pin
them: the preflight refusal, the two candidate envelopes, the verdict rules, the benchmark
statistics and the evidence schema. Lab code: it runs only inside the disposable VM the operator
approved, behind :func:`preflight`, and nothing in the app imports it.
"""

from __future__ import annotations

import math
import os
import random
import re
import subprocess
from pathlib import Path

MARKER = Path("/etc/battlelab-multiuser-p0")
#: Environment variable with extra hostnames to refuse (comma-separated), kept out of the repo.
REFUSE_HOSTS_ENV = "MULTIUSER_P0_REFUSE_HOSTS"
#: Groups that make an account administrator-equivalent; a test account must have none of them.
PRIVILEGED_GROUPS = frozenset({"sudo", "wheel", "admin", "docker", "lxd", "libvirt", "root"})
VERDICTS = ("pass", "fail", "unsupported", "not-run")
ATTACK_IDS = ("A1", "A2", "A3", "A4", "A5", "A6", "R1", "R2", "R3", "R4", "R5")
LAUNCH_BUDGET_MS = 300.0
FIRST_BYTE_BUDGET_MS = 50.0
THROUGHPUT_TOLERANCE = 0.03


class Refused(Exception):
    pass


# ---- preflight ----------------------------------------------------------------------------------


def preflight(
    *, marker: str | None, hostname: str, refuse_hosts: str, user: str, uid: int, groups: set[str]
) -> list[str]:
    """Every reason not to run here; empty means go. ``marker`` is the marker file's content
    (None when absent): one test-account name per line, the accounts this VM was set up with."""
    problems: list[str] = []
    if marker is None:
        problems.append(f"no {MARKER}: this is not the approved disposable VM")
        accounts: list[str] = []
    else:
        accounts = [line.strip() for line in marker.splitlines() if line.strip()]
        if len(accounts) < 2:
            problems.append(f"{MARKER} must name the two test accounts")
    refused = {h.strip().lower() for h in refuse_hosts.split(",") if h.strip()}
    if hostname.lower() in refused:
        problems.append(f"hostname {hostname!r} is on the refusal list")
    if uid == 0:
        problems.append("refusing to run as root")
    if accounts and user not in accounts:
        problems.append(f"{user!r} is not one of the VM's test accounts")
    if groups & PRIVILEGED_GROUPS:
        problems.append(f"{user!r} is in privileged groups {sorted(groups & PRIVILEGED_GROUPS)}")
    return problems


# ---- host facts ---------------------------------------------------------------------------------


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _version(argv: list[str]) -> str | None:
    try:
        # A literal argv from this module, never input: S603 does not apply.
        out = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return (
        (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    )


def host_facts() -> dict:
    """The facts the evidence freezes (contract §7). Measured here, never copied from elsewhere."""
    from landlock_exec import abi  # same directory; the CLI puts it on sys.path

    return {
        "kernel": os.uname().release,
        "landlock_abi": abi(),
        "lsm": _read("/sys/kernel/security/lsm"),
        "unprivileged_userns_clone": _read("/proc/sys/kernel/unprivileged_userns_clone"),
        "apparmor_restrict_unprivileged_userns": _read(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
        ),
        "yama_ptrace_scope": _read("/proc/sys/kernel/yama/ptrace_scope"),
        "bwrap": _version(["/usr/bin/bwrap", "--version"]),
        "systemd": _version(["/usr/bin/systemctl", "--version"]),
    }


# ---- the two candidate envelopes (contract §8) --------------------------------------------------


def _abs(path: str, what: str) -> str:
    if not os.path.isabs(path) or path != os.path.normpath(path):
        raise Refused(f"{what} must be a normalized absolute path: {path!r}")
    if path == "/":
        raise Refused(f"{what} may not be /")
    return path


def candidate_a_argv(
    *,
    workspace: str,
    home: str,
    agent_home: str,
    state_dir: str,
    sockets: list[str],
    root_links: dict[str, str],
    env: dict[str, str],
    landlock: list[str],
    command: list[str],
    unshare_net: bool,
) -> list[str]:
    """bubblewrap around the vendor CLI child, then Landlock, then the command (candidate A).

    ``home`` is the HOME the child sees: a tmpfs holding only ``agent_home`` (the one agent account
    fixed at creation) bound at it. ``root_links`` are the merged-/usr top-level symlinks
    (``/bin`` → ``usr/bin``). ``landlock`` is the ``landlock_exec.py`` prefix (interpreter, script
    and its ``--ro``/``--rw``/``--connect`` options); ``command`` is the vendor CLI's argv."""
    workspace, home = _abs(workspace, "workspace"), _abs(home, "home")
    agent_home, state_dir = _abs(agent_home, "agent_home"), _abs(state_dir, "state_dir")
    if not command or not os.path.isabs(command[0]):
        raise Refused("the command must start with an absolute path")
    argv = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
    ]
    if unshare_net:
        argv.append("--unshare-net")
    for top in ("/usr", "/etc", "/opt"):
        argv += ["--ro-bind-try", top, top]
    for link, target in sorted(root_links.items()):
        argv += ["--symlink", target, _abs(link, "root link")]
    # Mount targets inside the sandbox, not temporary files on the host (S108).
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]  # noqa: S108
    argv += ["--tmpfs", "/home"]
    argv += ["--tmpfs", "/run", "--dir", home, "--bind", agent_home, home]
    argv += ["--bind", state_dir, state_dir, "--bind", workspace, workspace]
    for sock in sockets:
        # The parent is created explicitly: it may sit in a fresh tmpfs (Hermes, review 8923).
        parent = _abs(os.path.dirname(_abs(sock, "socket")), "socket parent")
        argv += ["--dir", parent, "--bind", sock, sock]
    argv += ["--chdir", workspace, "--clearenv"]
    for key, value in sorted(env.items()):
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise Refused(f"bad environment key {key!r}")
        if key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            raise Refused(f"{key} reaches the user manager and is never passed to the child")
        argv += ["--setenv", key, value]
    argv += ["--setenv", "HOME", home, "--"]
    return argv + list(landlock) + list(command)


def candidate_b_properties(*, workspace: str, state_dir: str, app_socket: str) -> list[str]:
    """systemd unit sandboxing for the whole ``battlelab-native-<hex>`` unit (candidate B).
    Appended to ``native_containment.launch_argv``'s properties; which of them a user manager
    honours is exactly what P0b measures."""
    workspace, state_dir = _abs(workspace, "workspace"), _abs(state_dir, "state_dir")
    app_socket = _abs(app_socket, "app_socket")
    return [
        f"--property={p}"
        for p in (
            "PrivateUsers=yes",
            "PrivateTmp=yes",
            "PrivateIPC=yes",
            "ProtectSystem=strict",
            "ProtectHome=tmpfs",
            f"BindPaths={workspace} {state_dir}",
            f"BindReadOnlyPaths={app_socket}",
            "ProtectProc=invisible",
            "ProcSubset=pid",
            "RestrictNamespaces=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "SystemCallFilter=@system-service",
            "SystemCallArchitectures=native",
        )
    ]


# ---- verdicts (contract §9) ---------------------------------------------------------------------

#: Errors that mean the attack was stopped. ECONNREFUSED is deliberately absent: a refused TCP
#: connection proves the path to the service exists, which is what A5 forbids.
DENIAL_ERRORS = frozenset(
    {"EACCES", "EPERM", "ESRCH", "ENETUNREACH", "EHOSTUNREACH", "EROFS", "timeout"}
)
#: ENOENT means "not there". Inside a sandbox that is the intended denial for a path the envelope
#: hides, but only if the target was verified to exist OUTSIDE it first. Otherwise a canary that
#: was never created would read as a pass (Hermes, review 8923).
NOT_THERE = "ENOENT"


def verdict(
    *,
    expected: str,
    observed: str | None,
    detail: str = "",
    supported: bool = True,
    target_exists: bool = False,
) -> str:
    """One row's verdict. ``observed`` None means the probe did not run. ``target_exists`` says the
    harness checked, outside the envelope, that the row's target is really there."""
    if not supported:
        return "unsupported"
    if observed is None:
        return "not-run"
    if expected == "denied":
        if observed == "allowed":
            return "fail"
        code = detail.split()[-1] if detail.split() else ""
        if code == NOT_THERE:
            return "pass" if target_exists else "not-run"  # an unverified target proves nothing
        return "pass" if code in DENIAL_ERRORS else "fail"
    if expected == "allowed":
        return "pass" if observed == "allowed" else "fail"
    raise Refused(f"unknown expectation {expected!r}")


def _reachable_for_read(path: str) -> bool:
    """The path resolves (no dangling link, no loop) to something this process can read."""
    try:
        real = os.path.realpath(path, strict=True)
    except (OSError, RuntimeError):
        return False
    return os.access(real, os.R_OK)


def _reachable_for_write(path: str) -> bool:
    """The path resolves to a writable file, or does not exist yet under a writable directory.
    A dangling link is neither: it is refused, not followed."""
    if os.path.islink(path) and not os.path.exists(path):
        return False
    try:
        real = os.path.realpath(path, strict=True)
    except FileNotFoundError:
        parent = os.path.dirname(os.path.abspath(path))
        return os.path.isdir(parent) and os.access(parent, os.W_OK | os.X_OK)
    except (OSError, RuntimeError):
        return False
    return os.access(real, os.W_OK)


#: Plan sections whose target is a filesystem path, the key that names it, and how the host
#: verifies it is really reachable OUTSIDE the envelope (review 8928: presence is not enough; a
#: dangling symlink "exists" to lexists but proves nothing).
PATH_TARGETS = {
    "read": ("path", _reachable_for_read),
    "write": ("path", _reachable_for_write),
    "symlink": ("target", _reachable_for_read),
}


def prepare_plan(plan: dict) -> dict:
    """The plan with ``target_exists`` set on every filesystem row: whether the host, OUTSIDE the
    envelope, can actually reach that row's target (read it, or write it). It is what lets an
    ENOENT inside the envelope count as a denial (§9); anything this check cannot confirm stays
    False, so its ENOENT is ``not-run``. Run it before the envelope starts; the probe passes the
    flag through into its rows."""
    out = {key: [dict(item) for item in value] for key, value in plan.items()}
    for section, (key, reachable) in PATH_TARGETS.items():
        for item in out.get(section, []):
            item["target_exists"] = bool(reachable(item[key]))
    return out


def judge(row: dict, *, expected: str, supported: bool = True) -> dict:
    """A probe row with its verdict, using the ``target_exists`` the prepared plan carried."""
    return {
        **row,
        "verdict": verdict(
            expected=expected,
            observed=row.get("observed"),
            detail=row.get("detail", ""),
            supported=supported,
            target_exists=bool(row.get("target_exists")),
        ),
    }


def summarize(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Per attack ID, how many rows got each verdict. The four verdicts never merge."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = out.setdefault(row["id"], dict.fromkeys(VERDICTS, 0))
        counts[row["verdict"]] += 1
    return out


# ---- benchmark statistics (contract §7) ---------------------------------------------------------


def percentile(samples: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100]; the method the contract names."""
    if not samples:
        raise Refused("no samples")
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil(n * q / 100)
    return ordered[int(rank) - 1]


def launch_within_budget(baseline_ms: list[float], candidate_ms: list[float]) -> tuple[bool, float]:
    """Paired (ABAB) launches: the p95 of candidate minus baseline, against the budget."""
    if len(baseline_ms) != len(candidate_ms):
        raise Refused("launch samples must be paired")
    diffs = [c - b for b, c in zip(baseline_ms, candidate_ms, strict=True)]
    p95 = percentile(diffs, 95)
    return p95 <= LAUNCH_BUDGET_MS, p95


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def throughput_holds(
    baseline: list[float], candidate: list[float], *, seed: int = 1469, resamples: int = 2000
) -> tuple[bool, dict]:
    """No throughput loss: the 95% bootstrap CI of the median ratio includes 1.0 or lies above
    it, or the median ratio is within 3% of 1.0. Seeded: the same samples give the same answer."""
    if not baseline or not candidate:
        raise Refused("no samples")
    rng = random.Random(seed)  # noqa: S311 - a seeded bootstrap, not cryptography
    ratios = []
    for _ in range(resamples):
        b = _median([rng.choice(baseline) for _ in baseline])
        c = _median([rng.choice(candidate) for _ in candidate])
        ratios.append(c / b)
    ratios.sort()
    low, high = ratios[int(0.025 * resamples)], ratios[int(0.975 * resamples) - 1]
    point = _median(candidate) / _median(baseline)
    ok = high >= 1.0 or abs(point - 1.0) <= THROUGHPUT_TOLERANCE
    return ok, {"median_ratio": point, "ci95": [low, high]}


# ---- the evidence file (contract §7, §9) --------------------------------------------------------

REQUIRED_META = ("source_sha", "host", "backend", "policy", "probe_ids", "approval")
HOST_FACTS = ("kernel", "landlock_abi", "bwrap", "systemd")
#: The minimum raw samples per arm, by sample-name family (contract §7). A name outside these
#: families is refused, so every sample set is one the protocol defines.
SAMPLE_MINIMUMS = {
    "launch_": 20,
    "request_": 200,
    "stream_": 200,
    "first_byte_": 200,
    "throughput_": 200,
}


#: The arms a measured backend must carry: the launch, first-byte and throughput budgets (§6).
REQUIRED_ARMS = ("launch_", "first_byte_", "throughput_")


def _blank(value: object) -> bool:
    """None, an empty container, or a string that is empty once stripped."""
    if isinstance(value, str):
        return not value.strip()
    return value is None or (isinstance(value, list | dict) and not value)


def evidence_problems(evidence: dict) -> list[str]:
    """Why an evidence file is incomplete; empty means it may be attached. Values are checked, not
    only keys: an empty approval, a null host fact or a one-sample arm is incomplete."""
    problems: list[str] = []
    meta = evidence.get("meta") or {}
    for key in REQUIRED_META:
        if key not in meta:
            problems.append(f"meta.{key} is missing")
        elif _blank(meta[key]):
            problems.append(f"meta.{key} is empty")
    sha = str(meta.get("source_sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        problems.append("meta.source_sha must be a full 40-hex SHA")
    elif set(sha) == {"0"}:
        problems.append("meta.source_sha is the all-zero placeholder")
    host = meta.get("host") or {}
    for key in HOST_FACTS:
        if key not in host:
            problems.append(f"meta.host.{key} is missing")
        elif _blank(host[key]):
            problems.append(f"meta.host.{key} is empty")
    probe_ids = set(meta.get("probe_ids") or [])
    if not probe_ids <= set(ATTACK_IDS):
        problems.append(f"unknown probe ids {sorted(probe_ids - set(ATTACK_IDS))}")
    rows = evidence.get("rows") or []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        problems.append("rows must be a list of objects")
        rows = []
    for pid in sorted(probe_ids & set(ATTACK_IDS)):
        if not any(row.get("id") == pid for row in rows):
            problems.append(f"probe id {pid} is declared in meta.probe_ids but has no row")
    for i, row in enumerate(rows):
        if row.get("verdict") not in VERDICTS:
            problems.append(f"rows[{i}].verdict must be one of {VERDICTS}")
        if row.get("id") not in probe_ids:
            problems.append(f"rows[{i}].id {row.get('id')!r} is not in meta.probe_ids")
        if row.get("verdict") == "pass" and not row.get("evidence"):
            problems.append(f"rows[{i}] is a pass without evidence")
    samples_doc = evidence.get("samples")
    measured = any(row.get("verdict") != "unsupported" for row in rows)
    if measured:
        # A backend that ran must carry the arms every §6 budget is judged on. Only a backend
        # whose every row is `unsupported` (it could not run here at all) may carry none.
        if not isinstance(samples_doc, dict) or not samples_doc:
            problems.append("samples is missing or empty: raw samples are required (contract §7)")
            samples_doc = {}
        for family in REQUIRED_ARMS:
            if not any(name.startswith(family) for name in samples_doc):
                problems.append(f"samples has no {family}* arm; its budget cannot be judged")
    elif not isinstance(samples_doc, dict | type(None)):
        problems.append("samples must be an object")
        samples_doc = {}
    for name, samples in (samples_doc or {}).items():
        if not isinstance(samples, list) or not all(
            isinstance(s, int | float) and not isinstance(s, bool) and math.isfinite(s)
            for s in samples
        ):
            problems.append(f"samples.{name} must be raw numbers, all finite")
            continue
        family = next((f for f in SAMPLE_MINIMUMS if name.startswith(f)), None)
        if family is None:
            problems.append(
                f"samples.{name} is not a protocol sample family {sorted(SAMPLE_MINIMUMS)}"
            )
        elif len(samples) < SAMPLE_MINIMUMS[family]:
            problems.append(
                f"samples.{name} has {len(samples)} samples; the protocol needs at least "
                f"{SAMPLE_MINIMUMS[family]}"
            )
    return problems
