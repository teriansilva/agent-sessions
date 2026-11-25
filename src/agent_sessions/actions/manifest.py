"""The action manifest: a strict, data-only TOML validator (#2248 P1).

An action manifest DESCRIBES one step an automation can take: its name, its category, one line
saying what it does, its typed inputs and outputs, how far its effect reaches, what it needs, and
which reviewed executor carries it out. It can never add behaviour:

* an unknown field, at the top or in any input or output, refuses the whole manifest (a field a
  later contract adds may be a restriction, and ignoring a restriction widens what it can do);
* a field that would carry code, a command or an address (``code``, ``command``, ``url`` …) is
  refused by name, so the refusal says why;
* every enumerated value (category, effect, requirement, trigger, input and output type) must be a
  member of a set below, which is reviewed code;
* ``executor`` names a member of the closed executor table (`executors.EXECUTORS`), checked by the
  registry when it loads;
* free text (label, description, an input's label) is bounded, printable, and may not look like a
  URL, a template or a shell substitution.

The bundled manifests ship as package data (``actions/first_party/*.toml``), one file per action,
named by its id. Read ``docs/invariants/automations.md`` before changing what an action may do.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources

#: Manifest contract versions this build reads. A higher one is refused, never half-read.
CONTRACT_CURRENT = 1

ID_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
LABEL_MAX = 40
DESCRIPTION_MAX = 160
INPUTS_MAX = 16
OUTPUTS_MAX = 8
CHOICES_MAX = 8
MANIFEST_MAX_BYTES = 16 * 1024

#: The gallery's categories, in the order its chips show them, with their words.
CATEGORIES: dict[str, str] = {
    "battlelab": "BattleLab",
    "messaging": "Messaging",
    "files": "Files",
    "run_locally": "Run locally",
    "web": "Web",
}

#: How far an action's effect reaches, least to most. A CLASS, never a state: the gallery draws it
#: as pips (its level), never in a status colour.
EFFECTS: dict[str, tuple[int, str]] = {
    "read": (1, "Read"),
    "notify": (2, "Notify"),
    "write-battlelab": (3, "Write · BattleLab"),
    "write-external": (4, "Write · external"),
    "run-local": (5, "Run · local"),
}

#: What an action needs before it can run. The words are reviewed code; `registry.requirement`
#: checks the ones that can be checked without a choice the operator has not made yet.
REQUIREMENTS: dict[str, str] = {
    "api_agent": "an API agent that can start unattended",
    "project": "a project",
    "running_session": "a running API session to message",
    # #2248 P2
    "messenger": "a messenger (Telegram, Matrix or mail) that can send",
    "artifacts": "artifacts switched on in Settings",
    "memory": "project memory switched on for the project",
    # #2248 P3
    "containment": "a contained process scope for commands (systemd-run --user)",
}

#: The triggers that may start an action (`automations.TRIGGER_KINDS`).
TRIGGERS = ("once", "schedule", "manual", "webhook")

#: An input's type. Each is a kind of value the wizard knows how to ask for.
INPUT_TYPES = frozenset(
    {
        "agent",
        # #2248 P3: an argument list (each fixed or one typed delivery value), named environment
        # variables, a time limit, and the typed fields read from a command's last line.
        "arguments",
        "duration",
        "environment",
        "output_fields",
        "choice",
        # #2248 P2: a messenger, one of its saved recipients, an operator-set https destination,
        # typed body fields, and a file or folder inside a project.
        "connector",
        "destination",
        "fields",
        "file",
        "recipient",
        "folder",
        "message",
        "number",
        "project",
        "session",
        "specialist",
        "text",
        "yes_no",
    }
)
#: An output's type: what a later step could use.
OUTPUT_TYPES = frozenset(
    {
        "mission",
        "notification",
        "session",
        "message",
        "artifact",
        "note",
        "response",
        # #2248 P3: a command's exit code, and the typed fields it reports.
        "number",
        "fields",
    }
)

#: Field names that would carry behaviour. Refused by name at every level, before the unknown-field
#: check, so the refusal names what is wrong rather than only that something is unknown.
FORBIDDEN_FIELDS = frozenset(
    {
        "argv",
        "cmd",
        "code",
        "command",
        "endpoint",
        "entrypoint",
        "exec",
        "function",
        "module",
        "path",
        "program",
        "script",
        "url",
        "url_template",
    }
)
#: What free text may not contain: an address, a template slot or a shell substitution.
_SUSPECT_TEXT = re.compile(r"://|\{|\}|\$\(|`|\$\{|<|>|\\")

_TOP = frozenset(
    {
        "contract",
        "id",
        "label",
        "category",
        "description",
        "effect",
        "executor",
        "requires",
        "triggers",
        "inputs",
        "outputs",
    }
)
_TOP_REQUIRED = _TOP - {"requires", "inputs", "outputs"}
_INPUT = frozenset({"name", "type", "label", "required", "choices", "many"})
_INPUT_REQUIRED = frozenset({"name", "type", "label"})
_OUTPUT = frozenset({"name", "type", "label"})


class ManifestError(ValueError):
    """The manifest is refused. The message names the field, never echoes a long value."""


@dataclass(frozen=True)
class Input:
    name: str
    type: str
    label: str
    required: bool
    choices: tuple[str, ...]
    many: bool

    def public(self) -> dict:
        out: dict = {
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "required": self.required,
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.many:
            out["many"] = True
        return out


@dataclass(frozen=True)
class Output:
    name: str
    type: str
    label: str

    def public(self) -> dict:
        return {"name": self.name, "type": self.type, "label": self.label}


@dataclass(frozen=True)
class ActionManifest:
    id: str
    label: str
    category: str
    description: str
    effect: str
    executor: str
    requires: tuple[str, ...]
    triggers: tuple[str, ...]
    inputs: tuple[Input, ...]
    outputs: tuple[Output, ...]

    def public(self) -> dict:
        """The manifest's facts, as the gallery shows them. A manifest holds nothing secret."""
        return {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "effect": self.effect,
            "executor": self.executor,
            "triggers": list(self.triggers),
            "inputs": [i.public() for i in self.inputs],
            "outputs": [o.public() for o in self.outputs],
        }


def _fields(table: object, where: str, allowed: frozenset[str], required: frozenset[str]) -> dict:
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    bad = sorted(set(table) & FORBIDDEN_FIELDS)
    if bad:
        raise ManifestError(
            f"{where}{'.' if where != 'manifest' else ' field '}{bad[0]}: "
            "a manifest carries no code, command or address"
        )
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ManifestError(f"unknown field {unknown[0]!r} in {where}")
    missing = sorted(required - set(table))
    if missing:
        raise ManifestError(f"{where} is missing {missing[0]!r}")
    return table


def _text(value: object, field: str, cap: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    if len(value) > cap:
        raise ManifestError(f"{field} is longer than {cap} characters")
    if not value.isprintable():
        raise ManifestError(f"{field} must be printable text")
    if _SUSPECT_TEXT.search(value):
        raise ManifestError(f"{field} must be plain words, never an address, template or command")
    return value.strip()


def _id(value: object, field: str, rx: re.Pattern = ID_RE) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise ManifestError(f"{field} must be a lowercase id")
    return value


def _member(value: object, field: str, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ManifestError(f"{field} must be one of {sorted(allowed)}")
    return value


def _members(value: object, field: str, allowed, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty):
        raise ManifestError(f"{field} must be a {'' if empty else 'non-empty '}list")
    out = tuple(_member(v, field, allowed) for v in value)
    if len(set(out)) != len(out):
        raise ManifestError(f"{field} lists a value twice")
    return out


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{field} must be true or false")
    return value


def _input(raw: object, i: int) -> Input:
    where = f"inputs[{i}]"
    t = _fields(raw, where, _INPUT, _INPUT_REQUIRED)
    kind = _member(t["type"], f"{where}.type", INPUT_TYPES)
    choices: tuple[str, ...] = ()
    if "choices" in t:
        if kind != "choice":
            raise ManifestError(f"{where}.choices go with a choice input only")
        c = t["choices"]
        if not isinstance(c, list) or not c or len(c) > CHOICES_MAX:
            raise ManifestError(f"{where}.choices must list 1 to {CHOICES_MAX} values")
        choices = tuple(_id(v, f"{where}.choices") for v in c)
        if len(set(choices)) != len(choices):
            raise ManifestError(f"{where}.choices lists a value twice")
    elif kind == "choice":
        raise ManifestError(f"{where} is a choice and needs choices")
    many = _bool(t.get("many", False), f"{where}.many")
    if many and kind != "choice":
        raise ManifestError(f"{where}.many goes with a choice input only")
    return Input(
        name=_id(t["name"], f"{where}.name", NAME_RE),
        type=kind,
        label=_text(t["label"], f"{where}.label", LABEL_MAX),
        required=_bool(t.get("required", False), f"{where}.required"),
        choices=choices,
        many=many,
    )


def _output(raw: object, i: int) -> Output:
    where = f"outputs[{i}]"
    t = _fields(raw, where, _OUTPUT, _OUTPUT)
    return Output(
        name=_id(t["name"], f"{where}.name", NAME_RE),
        type=_member(t["type"], f"{where}.type", OUTPUT_TYPES),
        label=_text(t["label"], f"{where}.label", LABEL_MAX),
    )


def _list(value: object, field: str, cap: int) -> list:
    if not isinstance(value, list) or len(value) > cap:
        raise ManifestError(f"{field} must be a list of at most {cap}")
    return value


def validate(doc: object) -> ActionManifest:
    """A parsed TOML document → `ActionManifest`, or `ManifestError`.

    Whether ``executor`` names a reviewed executor is the registry's check (`registry.load`): the
    manifest module stays importable without the code that runs actions."""
    d = _fields(doc, "manifest", _TOP, _TOP_REQUIRED)
    contract = d["contract"]
    if not isinstance(contract, int) or isinstance(contract, bool):
        raise ManifestError("contract must be an integer")
    if contract > CONTRACT_CURRENT:
        raise ManifestError("this action needs a newer BattleLab (contract too new)")
    if contract != CONTRACT_CURRENT:
        raise ManifestError("contract is not one this build reads")
    inputs = tuple(
        _input(r, i) for i, r in enumerate(_list(d.get("inputs", []), "inputs", INPUTS_MAX))
    )
    names = [i.name for i in inputs]
    if len(set(names)) != len(names):
        raise ManifestError("two inputs share a name")
    outputs = tuple(
        _output(r, i) for i, r in enumerate(_list(d.get("outputs", []), "outputs", OUTPUTS_MAX))
    )
    if len({o.name for o in outputs}) != len(outputs):
        raise ManifestError("two outputs share a name")
    return ActionManifest(
        id=_id(d["id"], "id"),
        label=_text(d["label"], "label", LABEL_MAX),
        category=_member(d["category"], "category", CATEGORIES),
        description=_text(d["description"], "description", DESCRIPTION_MAX),
        effect=_member(d["effect"], "effect", EFFECTS),
        executor=_id(d["executor"], "executor"),
        requires=_members(d.get("requires", []), "requires", REQUIREMENTS, empty=True),
        triggers=_members(d["triggers"], "triggers", TRIGGERS),
        inputs=inputs,
        outputs=outputs,
    )


def parse(data: bytes) -> ActionManifest:
    """Manifest bytes → `ActionManifest`, or `ManifestError`."""
    if len(data) > MANIFEST_MAX_BYTES:
        raise ManifestError("the manifest is too large")
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"not valid TOML ({type(e).__name__})") from None
    return validate(doc)


def bundled() -> list[ActionManifest]:
    """The first-party manifests shipped in the package, in file-name order. A broken bundled
    manifest is a release defect and raises."""
    out: list[ActionManifest] = []
    root = resources.files(__package__).joinpath("first_party")
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".toml"):
            continue
        m = parse(entry.read_bytes())
        if f"{m.id}.toml" != entry.name or any(o.id == m.id for o in out):
            raise ManifestError(f"bundled manifest {entry.name} is misnamed or duplicated")
        out.append(m)
    return out
