"""The substituted service manager of the scripted-native tests (#1278).

``FakeHost`` stands in for the systemd user manager ONLY: it starts the exact contained command
``systemd-run`` would run, as a plain child, and reports unit and cgroup evidence from that
child's liveness. Everything else is the real thing: the real ``native_worker`` process per
generation, the real codecs and the real private socket.

It lives in a module of its own because two harnesses use it: ``test_native_runtime.py`` (and the
tests that take its ``host`` fixture) and ``tests/appmode/federation_instance.py``, which hosts a
real API session in a running app for the two-instance browser specs. Importing a test module
from a server process would pull pytest and every fixture module in with it.
"""

from __future__ import annotations

import os
import subprocess
import uuid

from agent_sessions import native_containment, native_runtime


class FakeHost(native_runtime.Host):
    """Runs the exact contained command as a plain child; evidence follows its liveness."""

    def __init__(self, runtime_dir: str):
        self.runtime = runtime_dir
        self.procs: dict[str, subprocess.Popen] = {}
        self.invocations: dict[str, str] = {}
        self.launches: list[list[str]] = []

    def runtime_dir(self) -> str:
        return self.runtime

    def available(self) -> str | None:
        return None

    def launch(self, argv: list[str]) -> bool:
        self.launches.append(argv)
        assert argv[:2] == ["/usr/bin/systemd-run", "--user"]
        assert "--property=KillMode=control-group" in argv
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        assert unit not in self.procs, "a unit name was reused"
        command = argv[argv.index("--") + 1 :]
        assert command[:2] == ["/usr/bin/env", "-i"]
        env, index = {}, 2
        while "=" in command[index] and not command[index].startswith("/"):
            key, value = command[index].split("=", 1)
            env[key] = value
            index += 1
        cwd = next(a.split("=", 1)[1] for a in argv if a.startswith("--working-directory="))
        self.procs[unit] = subprocess.Popen(command[index:], env=env, cwd=cwd)
        self.invocations[unit] = uuid.uuid4().hex
        return True

    def _alive(self, unit: str) -> bool:
        proc = self.procs.get(unit)
        return proc is not None and proc.poll() is None

    def show(self, worker):
        unit = worker.unit
        if not self._alive(unit):
            return native_containment.UnitObservation(
                unit, "not-found", "inactive", "dead", None, None, 0, ""
            )
        return native_containment.UnitObservation(
            unit,
            "loaded",
            "active",
            "running",
            self.invocations[unit],
            f"/user.slice/{unit}",
            self.procs[unit].pid,
            "success",
        )

    def cgroup(self, control_group: str):
        unit = os.path.basename(control_group)
        state = "populated" if self._alive(unit) else "absent"
        return native_containment.CgroupObservation(control_group, state)

    def stop(self, argv: list[str]) -> bool:
        proc = self.procs.get(argv[-1])
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(10)
        return True

    def worker_running(self, worker, pid) -> bool:
        proc = self.procs.get(worker.unit)
        return proc is not None and proc.poll() is None and proc.pid == pid

    def kill(self, worker_id: str) -> None:
        proc = self.procs[native_containment.WorkerIdentity(worker_id).unit]
        proc.kill()
        proc.wait(10)

    def close(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
