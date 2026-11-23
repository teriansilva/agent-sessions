"""Run a command (#2248 P3): an operator-authored argument list, run contained in a project.

``inputs = {project_id, argv, env?, timeout_minutes?, outputs?}``:

* ``argv`` — the program and its arguments, one element per argument: ``{value, option?}`` (fixed)
  or ``{from: field}`` (a typed value from the webhook delivery, filling that WHOLE argument).
  ``argv[0]`` is the program: always fixed, an absolute path or a name looked up in the run's fixed
  ``PATH``, never a shell or a program that runs other commands from its arguments
  (`run_local.LAUNCHERS`: write a script and use Run a script). An element marked ``option`` and
  the element right after it are always fixed.
* ``env`` — up to 16 variables, ``{name, value}`` or ``{name, from}`` (`run_local.validate_env`).
* ``timeout_minutes`` — 1 to 30 (5 unless set). ``outputs`` — typed fields read from a JSON object
  on the last line of stdout.

Pinned at consent: the project's folder, the run-as user, and the program's real path and sha256
(and, when the program is itself a ``#!`` script, its interpreter chain). Everything here is in
the consent's target: any change is a new approval. See `run_local` for the run.
"""

from __future__ import annotations

from .. import automations as model
from . import run_local as rl

ID = "run_command"
LABEL = "Run a command"
WEBHOOK = True
MESSAGE = False
SECRET_REFUSAL = ""
TARGET_WORDS = {
    "project_id": "a different project",
    "folder": "the project's folder changed",
    "run_as": "a different user",
    "argv": "a different command",
    "program": "a different program, or the program changed",
    "interpreter": "the program's interpreter changed",
    "env": "different environment variables",
    "timeout_minutes": "a different time limit",
    "outputs": "different output fields",
}
_INPUTS = {"project_id", "argv", "env", "timeout_minutes", "outputs"}


# ---- validation and save ------------------------------------------------------------------------


def validate_inputs(inputs: dict, mapped: dict) -> dict:
    model._obj(inputs, "a run_command action's inputs", _INPUTS, {"project_id", "argv"})
    pid = inputs["project_id"]
    if not isinstance(pid, str) or not pid or len(pid) > 200:
        raise model.AutomationError("project_id must be a project id")
    argv = rl.validate_elements(inputs["argv"], "argv", mapped, first_fixed=True)
    rl.validate_program_word(argv[0]["value"])
    why = rl.code_slot_problem(rl.program_names(argv[0]["value"]), argv[1:])
    if why:
        raise model.AutomationError(why)
    env = rl.validate_env(inputs.get("env"), mapped)
    timeout = rl.validate_timeout(inputs.get("timeout_minutes"))
    outputs = rl.validate_outputs(inputs.get("outputs"))
    rl.check_mapping_used(mapped, argv, env)
    return {
        "project_id": pid,
        "argv": argv,
        "env": env,
        "timeout_minutes": timeout,
        "outputs": outputs,
    }


def mappable(_inputs: dict) -> None:
    return None  # checked by `validate_inputs`: exactly the names its argument slots use


def check_save(config: dict) -> None:
    inp = config["action"]["inputs"]
    why = rl.containment_problem()
    if why:
        raise model.AutomationError(why)
    try:
        cwd = rl.folder_pin(inp["project_id"])
    except model.PinsUnavailable as e:
        raise model.AutomationError(str(e), status=503) from None
    if cwd.get("missing"):
        raise model.AutomationError(str(cwd["missing"]))
    problem = rl.folder_problem(cwd.get("real"))
    if problem:
        raise model.AutomationError(problem)
    try:
        real = rl.resolve_program(inp["argv"][0]["value"])
        rl.file_pin(real, "the program")
        rl.interpreter_chain(rl._head(real))
    except rl.Refused as r:
        raise model.AutomationError(r.reason) from None
    # The REAL program too: `python3` → python3.12, a link named `tool` → awk.
    why = rl.code_slot_problem(rl.program_names(inp["argv"][0]["value"], real), inp["argv"][1:])
    if why:
        raise model.AutomationError(why)
    # The program (argv[0]) is a name or a path, resolved and pinned above; the rest are values.
    rl.check_fixed_values(inp["argv"][1:], inp["env"])


# ---- pins, scope, consent -----------------------------------------------------------------------


def pins(action: dict) -> dict:
    inp = action["inputs"]
    cwd = rl.folder_pin(inp["project_id"])
    if cwd.get("missing"):
        return {"missing": str(cwd["missing"])}
    try:
        real = rl.resolve_program(inp["argv"][0]["value"])
        program = rl.file_pin(real, "the program")
        interpreter = rl.interpreter_chain(rl._head(real))
    except rl.Refused as r:
        return {"missing": r.reason}
    return {"cwd": cwd, "run_as": rl.run_as(), "program": program, "interpreter": interpreter}


def drift(stored: dict, current: dict) -> str:
    if stored.get("cwd") != current.get("cwd"):
        return "the project's folder changed since you approved this automation"
    if stored.get("run_as") != current.get("run_as"):
        return "the user it runs as changed since you approved this automation"
    sp, cp = stored.get("program") or {}, current.get("program") or {}
    if sp.get("real") != cp.get("real"):
        return f"the program is a different file now ({cp.get('real')})"
    if sp.get("sha256") != cp.get("sha256"):
        return rl.changed_words(f"the program {sp.get('real')}", sp, cp)
    if stored.get("interpreter") != current.get("interpreter"):
        return rl._interp_words(stored.get("interpreter") or [], current.get("interpreter") or [])
    return "what this command is bound to changed since you approved it"


def target(action: dict, pins_: dict) -> dict:
    inp = action["inputs"]
    p = pins_.get("plugin") or {}
    who = p.get("run_as") or {}
    return {
        "project_id": inp["project_id"],
        "folder": (p.get("cwd") or {}).get("real"),
        "run_as": {"uid": who.get("uid"), "user": who.get("user")},
        "argv": inp["argv"],
        "program": p.get("program"),
        "interpreter": p.get("interpreter") or [],
        "env": inp["env"],
        "timeout_minutes": inp["timeout_minutes"],
        "outputs": inp["outputs"],
    }


def data(_action: dict) -> dict:
    return {}  # every input is in the target: any change is a new approval


def lines(scope: dict) -> list[str]:
    t = scope.get("target") or {}
    mapping = _mapping_of(scope)
    argv = t.get("argv") if isinstance(t.get("argv"), list) else [t.get("argv")]
    chain = (
        t.get("interpreter") if isinstance(t.get("interpreter"), list) else [t.get("interpreter")]
    )
    out = [
        "Runs this command: [" + ", ".join(rl.show_element(e, mapping) for e in argv) + "]",
        f"The program is {rl.pin_words(t.get('program'))}",
    ]
    for i in chain:
        out.append(f"Through its interpreter {rl.pin_words(i)}")
    out.append(
        "If the program or its interpreter changes, or the project's folder moves, it asks for "
        "your approval again"
    )
    out.extend(rl.common_lines(t, mapping))
    return out


def _mapping_of(scope: dict) -> dict:
    """The delivery mapping, for the type words of a delivered slot (the scope's ``mapped``
    ladder holds ``field<-path:type`` strings)."""
    out: dict = {}
    for line in scope.get("mapped") or []:
        if isinstance(line, str) and "<-" in line and ":" in line:
            name, rest = line.split("<-", 1)
            path, typ = rest.rsplit(":", 1)
            out[name] = {"path": path, "type": typ.split("[", 1)[0]}
    return out


def fill(action: dict, delivered: dict) -> dict:
    """This run's delivered values, kept beside the inputs (never written into an element)."""
    return {**action, "inputs": {**action["inputs"], "delivered": dict(delivered)}}


# ---- the run ------------------------------------------------------------------------------------


def effect(run: dict, config: dict, pins_: dict) -> tuple[str, str]:
    """BLOCKING, under the effect lock, after the authority read."""
    inp = config["action"]["inputs"]
    pinned = pins_.get("plugin") or {}
    delivered = inp.get("delivered") or {}

    def launch_of() -> rl.Launch:
        real = _checked_folder(inp["project_id"], pinned)
        program_pin = pinned.get("program") or {}
        path = rl.resolve_program(inp["argv"][0]["value"])
        if path != program_pin.get("real"):
            raise rl.Refused(f"the program is a different file now ({path})", drift=True)
        fd, st = rl._open_regular(path, "the program")
        try:
            digest = rl._hash_fd(fd, rl.PROGRAM_MAX_BYTES)
            head = rl.os.pread(fd, rl.SHEBANG_MAX + 3, 0)
        finally:
            rl.os.close(fd)
        if digest != program_pin.get("sha256"):
            raise rl.Refused(
                rl.changed_words(f"the program {path}", program_pin, {"sha256": digest}),
                drift=True,
            )
        rl.check_interpreters(head, pinned.get("interpreter") or [])
        # Checked again here: a stored config is never trusted to have passed the save.
        why = rl.code_slot_problem(rl.program_names(inp["argv"][0]["value"], path), inp["argv"][1:])
        if why:
            raise rl.Refused(why)
        args = rl.build_args(inp["argv"][1:], delivered)
        env = rl.build_env(inp["env"], delivered)
        checked = rl._identity(st)

        def recheck() -> None:
            # Right before the spawn: the path still names the very file that was hashed.
            try:
                now = rl._identity(rl.os.stat(path))
            except OSError:
                now = None
            if now != checked:
                raise rl.Refused(f"the program {path} was replaced during the launch", drift=True)

        return rl.Launch(
            payload=[path, *args], env=env, cwd=real, shown=[path, *args], recheck=recheck
        )

    return rl.run_effect(
        run,
        launch_of=launch_of,
        record={"program": (pinned.get("program") or {}).get("real")},
        minutes=inp["timeout_minutes"],
        outputs=inp["outputs"],
    )


def _checked_folder(project_id: str, pinned: dict) -> str:
    """The project's folder NOW: equal to the pinned identity, and inside the boundary."""
    try:
        now = rl.folder_pin(project_id)
    except model.PinsUnavailable as e:
        raise rl.Refused(f"the project could not be resolved ({e})") from None
    if now != pinned.get("cwd") or not now.get("real"):
        raise rl.Refused("the project's folder changed since you approved this", drift=True)
    problem = rl.folder_problem(now["real"])
    if problem:
        raise rl.Refused(problem)
    if pinned.get("run_as") != rl.run_as():
        raise rl.Refused("the user it runs as changed since you approved this", drift=True)
    return now["real"]
