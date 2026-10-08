"""Pure containment contracts; these tests never launch systemd or an agent."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_sessions import native_containment as containment

WORKER = containment.WorkerIdentity("00000000-0000-4000-8000-000000000001")
INVOCATION = "1234567890abcdef1234567890abcdef"
CGROUP = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{WORKER.unit}"


def properties(**changes):
    values = {
        "Id": WORKER.unit,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "InvocationID": INVOCATION,
        "ControlGroup": CGROUP,
        "MainPID": "4321",
        "Result": "success",
    }
    values.update(changes)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def observed(**changes):
    return containment.parse_show(WORKER, properties(**changes))


def pinned():
    return containment.capture(WORKER, observed())


def group(state):
    return containment.CgroupObservation(CGROUP, state)


def test_launch_has_one_contained_entrypoint_and_private_output():
    argv = containment.launch_argv(
        WORKER,
        python="/releases/build-one/venv/bin/python",
        state_dir="/private/native state",
        home="/home/operator",
        runtime_dir="/run/user/1000",
    )
    assert argv[:3] == ["/usr/bin/systemd-run", "--user", "--quiet"]
    assert f"--unit={WORKER.unit}" in argv
    assert "--scope" not in argv and "--pty" not in argv
    assert "--expand-environment=no" in argv
    assert "--working-directory=/private/native state" in argv
    expected = {
        "ExitType=main",
        "KillMode=control-group",
        "Restart=no",
        "SendSIGKILL=yes",
        "RuntimeMaxSec=86400",
        "TasksMax=4096",
        "MemoryMax=8G",
        "Delegate=no",
        "StandardInput=null",
        "StandardOutput=null",
        "StandardError=null",
    }
    assert {"--property=" + value for value in expected} <= set(argv)
    assert argv[argv.index("--") + 1 :] == [
        "/usr/bin/env",
        "-i",
        "HOME=/home/operator",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        "/releases/build-one/venv/bin/python",
        "-I",
        "-m",
        "agent_sessions.native_worker",
        "--worker-id",
        WORKER.worker_id,
        "--state-dir",
        "/private/native state",
    ]


def test_launch_does_not_copy_process_environment_or_offer_a_passthrough(monkeypatch):
    monkeypatch.setenv("VENDOR_API_KEY", "synthetic-secret")
    monkeypatch.setenv("AGENT_SESSIONS_SESSION_SCOPES", "0")
    argv = containment.launch_argv(WORKER, python="/bin/python", state_dir="/state", home="/home")
    assert argv[0] == "/usr/bin/systemd-run"
    assert not any("synthetic-secret" in arg or "VENDOR_API_KEY" in arg for arg in argv)
    assert not any(arg.startswith("DBUS_SESSION_BUS_ADDRESS=") for arg in argv)


@pytest.mark.parametrize("field", ["python", "state_dir", "home", "runtime_dir"])
@pytest.mark.parametrize("path", ["relative", "/a/../b", "/a/%n", "/a\n/b", "//tmp/a", "/a\0b"])
def test_launch_rejects_path_reinterpretation(field, path):
    kwargs = {"python": "/bin/python", "state_dir": "/state", "home": "/home"}
    kwargs[field] = path
    with pytest.raises(containment.ContainmentError):
        containment.launch_argv(WORKER, **kwargs)


@pytest.mark.parametrize(
    "path", ["/tmp/run", "/run/user/1000;tcp:host=remote", "/run/user/1000,other=x"]
)
def test_runtime_bus_address_cannot_add_another_transport(path):
    with pytest.raises(containment.ContainmentError):
        containment.launch_argv(
            WORKER, python="/bin/python", state_dir="/state", home="/home", runtime_dir=path
        )


@pytest.mark.parametrize(
    "value",
    [
        "--help",
        "../unit",
        "00000000000040008000000000000001",
        "00000000-0000-1000-8000-000000000001",
        None,
    ],
)
def test_worker_unit_cannot_be_supplied_as_an_arbitrary_name(value):
    with pytest.raises(containment.ContainmentError):
        containment.WorkerIdentity(value)


def test_each_minted_generation_has_a_new_full_uuid_unit():
    first, second = containment.WorkerIdentity.mint(), containment.WorkerIdentity.mint()
    assert first != second and first.unit != second.unit
    assert first.worker_id.replace("-", "") in first.unit
    assert containment.show_argv(first)[-2:] == ["--", first.unit]


def test_complete_observation_captures_exact_invocation_and_cgroup():
    result = pinned()
    assert result.worker == WORKER
    assert result.invocation_id == INVOCATION
    assert result.control_group == CGROUP
    assert containment.stop_argv(result, observed()) == [
        "/usr/bin/systemctl",
        "--user",
        "stop",
        "--",
        WORKER.unit,
    ]


@pytest.mark.parametrize(
    "text",
    [
        "",
        properties() + "InvocationID=" + INVOCATION + "\n",
        properties(Id="other.service"),
        properties(ControlGroup="/user.slice/other.service"),
        properties(InvocationID="0" * 32),
        properties(InvocationID="not-an-invocation"),
        properties(MainPID="-1"),
        properties(LoadState="error"),
        properties(ActiveState="unexpected"),
        properties() + "Unexpected=value\n",
        "x" * (containment.MAX_SHOW_BYTES + 1),
    ],
)
def test_manager_queries_fail_closed_when_malformed_incomplete_or_foreign(text):
    with pytest.raises(containment.ContainmentError):
        containment.parse_show(WORKER, text)


def test_unsuccessful_query_is_not_a_missing_unit_observation():
    with pytest.raises(containment.ContainmentError):
        containment.parse_show(WORKER, properties(LoadState="not-found"), returncode=1)
    with pytest.raises(containment.ContainmentError):
        containment.capture(WORKER, observed(InvocationID=""))


@pytest.mark.parametrize("text", ["", "populated 2\n", "populated 0\npopulated 1\n", "0\n"])
def test_recursive_cgroup_population_needs_a_definite_bounded_observation(text):
    with pytest.raises(containment.ContainmentError):
        containment.parse_cgroup_events(CGROUP, text)


def test_cgroup_population_includes_descendants_after_main_process_exit():
    evidence = containment.parse_cgroup_events(CGROUP, "populated 1\nfrozen 0\n")
    assert evidence.state == "populated"
    exited = observed(ActiveState="failed", SubState="failed", MainPID="0", Result="exit-code")
    assert containment.classify(pinned(), exited, evidence, start_closed=True) == "live"


@pytest.mark.parametrize("cgroup", [None, group("unknown")])
def test_terminal_status_or_eof_without_population_proof_is_unknown(cgroup):
    stopped = observed(ActiveState="inactive", SubState="dead", MainPID="0")
    assert containment.classify(pinned(), stopped, cgroup, start_closed=True) == "unknown"
    assert containment.classify(pinned(), None, cgroup, start_closed=True) == "unknown"


@pytest.mark.parametrize("state", ["empty", "absent"])
def test_gone_requires_terminal_matching_generation_and_a_closed_drained_launch_gate(state):
    stopped = observed(ActiveState="inactive", SubState="dead", MainPID="0")
    assert containment.classify(pinned(), stopped, group(state), start_closed=False) == "unknown"
    assert containment.classify(pinned(), stopped, group(state), start_closed=True) == "gone"
    assert containment.classify(pinned(), observed(), group(state), start_closed=True) == "unknown"


def test_collected_unit_only_proves_gone_with_pinned_empty_group_and_closed_launch_gate():
    missing = observed(
        LoadState="not-found",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        InvocationID="",
        ControlGroup="",
    )
    assert containment.classify(pinned(), missing, None, start_closed=True) == "unknown"
    assert containment.classify(pinned(), missing, group("absent"), start_closed=False) == "unknown"
    assert containment.classify(pinned(), missing, group("absent"), start_closed=True) == "gone"
    assert containment.classify(pinned(), missing, group("populated"), start_closed=True) == "live"
    with pytest.raises(containment.ContainmentError):
        containment.stop_argv(pinned(), missing)


@pytest.mark.parametrize(
    "change",
    [
        {"invocation_id": "abcdef1234567890abcdef1234567890"},
        {"unit": "other.service"},
        {"control_group": "/another/group"},
    ],
)
def test_replaced_invocation_is_never_stopped_or_reported_gone(change):
    replaced = replace(observed(ActiveState="inactive", MainPID="0"), **change)
    with pytest.raises(containment.ContainmentError, match="unverified"):
        containment.stop_argv(pinned(), replaced)
    assert containment.classify(pinned(), replaced, group("empty"), start_closed=True) == "unknown"


def test_unrelated_empty_cgroup_is_not_proof_of_cleanup():
    stopped = observed(ActiveState="inactive", SubState="dead", MainPID="0")
    unrelated = containment.CgroupObservation("/another/empty", "empty")
    assert containment.classify(pinned(), stopped, unrelated, start_closed=True) == "unknown"
