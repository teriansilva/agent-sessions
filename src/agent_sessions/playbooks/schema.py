"""The playbook bundle format's closed value space (#1096 §2/§3/§5, #1190).

Everything a bundle may SAY is named here: the format version, the block vocabularies, the
per-probe-kind output slots, and how a probe argument may be filled. A bundle selects from these
tables; it never adds to them. The tables that already exist elsewhere are imported, never copied:
probe kinds and their argument contracts are `missions.PROBE_KINDS` / `missions.PROBE_ARG_SCHEMA`,
variable names are `templates.FIELD_NAME_RE`, engine and model reference shapes are the plugin
manifest's.
"""

from __future__ import annotations

import re

from .. import missions, prefs, template_send, templates
from ..plugins import kinds as plugin_kinds
from ..plugins import manifest as plugin_manifest

#: Format 2 permits proposed target defaults/choices, armed only by deployment review. Format 1
#: retains its original validation; reading/saving never silently upgrades a document.
FORMAT = 2
SUPPORTED_FORMATS = frozenset({1, 2})
MIGRATIONS: dict = {}

# --- identities ----------------------------------------------------------------------------------

PLAYBOOK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
#: Flows, runbooks and templates are named by their FILE STEM, so the id is also a filename.
FILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
#: A step id also appears inside `{{steps.<id>.<slot>}}` and later becomes a session's `role`.
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: A free domain tag (`development`, `documents`, `mail`, ...): a tag, never a code path.
DOMAIN_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
CONNECTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VERSION_RE = plugin_manifest._SEMVERISH_RE
#: Engine and model are REFERENCES here — validated for shape only. Resolution against the
#: roster is P2/P5 (#1191/#1194); an engine or model this host does not have is *unresolved*, not
#: an error, so a bundle stays portable between hosts.
ENGINE_REF_RE = plugin_manifest._ID_RE
MODEL_REF_RE = plugin_manifest._MODEL_ID_RE
MODEL_DEFAULT = "default"
BINARY_NAME_RE = plugin_manifest._BIN_NAME_RE
VARIABLE_NAME_RE = templates.FIELD_NAME_RE

#: Checklist item keys follow the checklist store's own key rules, so a flow's items can become a
#: mission's objectives (P4) without a second notion of a valid key.
ITEM_KEY_RE = prefs._PLAYBOOK_ID_RE
ITEM_KEY_MAX = prefs.PLAYBOOK_ID_MAX
ITEM_TITLE_MAX = prefs.PLAYBOOK_TITLE_MAX
ITEM_KEY_RESERVED_PREFIXES = (missions.NOTE_KEY_PREFIX, missions.GOAL_KEY_PREFIX)
ITEM_KEY_RESERVED = frozenset({missions.DONE_AS_INSTRUCTED_KEY})
ITEM_KEY_FORBIDDEN_SUBSTRING = missions.MINTED_KEY_SEP

# --- bounds --------------------------------------------------------------------------------------

MAX_FLOWS = 20
MAX_STEPS = 30
MAX_ITEMS_PER_STEP = 12
MAX_VARIABLES = 32
MAX_CONNECTIONS = 8
MAX_MATERIALS = 200
MAX_RUNBOOKS = 32
MAX_TEMPLATES = 32
MAX_RITUALS = 16
MAX_AFTER = MAX_STEPS
MAX_DISTINCT_FROM = 8
MAX_SKILLS = 8
MAX_BINARIES = 16
MAX_CHOICES = 32
MAX_BAIL = 8
REWORK_ROUNDS_MIN = 1
REWORK_ROUNDS_MAX = 10

LABEL_MAX = templates.LABEL_MAX
NAME_MAX = templates.NAME_MAX
HELP_MAX = 300
SUMMARY_MAX = 300
DEFAULT_MAX = templates.DEFAULT_MAX
BRIEF_MAX = missions.BRIEF_MAX
BAIL_MAX = 200
INT_ABS_MAX = templates.NUMBER_MAX_INT

#: The tree walk's bounds. A bundle is a tree someone else authored, so every one is enforced
#: while walking, before a byte of it is parsed.
MAX_TREE_ENTRIES = 1000
MAX_TREE_DEPTH = 8
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_TOML_BYTES = 64 * 1024
MAX_RUNBOOK_BYTES = 256 * 1024
MAX_README_BYTES = 64 * 1024
MAX_BUNDLES = 100

# --- the bundle tree -----------------------------------------------------------------------------

MANIFEST_NAME = "playbook.toml"
README_NAME = "README.md"
MATERIALS_DIR = "template"
RUNBOOKS_DIR = "runbooks"
FLOWS_DIR = "flows"
TEMPLATES_DIR = "templates"
#: What may sit at a bundle's root. Anything else is refused: an unknown entry is either a mistake
#: or something a later contract means, and neither should be silently carried.
ROOT_ENTRIES = frozenset(
    {MANIFEST_NAME, README_NAME, MATERIALS_DIR, RUNBOOKS_DIR, FLOWS_DIR, TEMPLATES_DIR}
)
#: One path segment: no glob, no separator, no control character. `.git` is refused separately
#: (case-insensitively), because nothing may ever land in git metadata (#863 §2).
PATH_SEG_RE = plugin_manifest._PATH_SEG_RE
PATH_MAX = 256

# --- manifest blocks -----------------------------------------------------------------------------

VARIABLE_KINDS = templates.FIELD_KINDS  # ("text", "secret") — #1090's field kinds, unchanged
#: #863's extra types. They validate a TEXT value; they add no kind (#1096 §3).
VARIABLE_TYPES = frozenset({"text", "url", "enum", "bool", "int", "path"})

CONNECTION_KINDS = frozenset({"forge", "http-endpoint", "filesystem-root", "opaque"})
#: kind -> {param: (required?, the variable type the param must reference)}. A connection param
#: is ALWAYS a whole `{{variable}}` reference — a literal would be a URL authority or a path the
#: bundle chose, which is exactly what the closed value space forbids.
CONNECTION_PARAMS: dict[str, dict[str, tuple[bool, str]]] = {
    "forge": {"url": (True, "url")},
    "http-endpoint": {"url": (True, "url")},
    "filesystem-root": {"path": (True, "path")},
    "opaque": {},
}
#: A connection's `verify` probe, from its kind's fixed set (#863 §7a). Metadata here: P2 runs
#: them, operator-initiated only.
CONNECTION_VERIFY: dict[str, frozenset[str]] = {
    "forge": frozenset({"none", "api"}),
    "http-endpoint": frozenset({"none", "status"}),
    "filesystem-root": frozenset({"none", "exists"}),
    "opaque": frozenset({"none"}),
}
assert set(CONNECTION_PARAMS) == set(CONNECTION_VERIFY) == CONNECTION_KINDS

DISPOSITIONS = frozenset({"managed", "seed", "reference"})
MATERIAL_FILE = "file"
MATERIAL_ALIAS = "instruction-alias"
MATERIAL_KINDS = frozenset({MATERIAL_FILE, MATERIAL_ALIAS})

#: What a playbook may REQUEST. Requested, never self-granted: the operator grants each one per
#: project, default-deny (#1096 §1). Unknown keys are refused, so a later capability is a new
#: entry here, never something a bundle invents.
REQUESTABLE_CAPABILITIES = frozenset({"orchestrator_input", "unattended_start", "shared_memory"})
#: `requires.capabilities` names ENGINE capabilities, from the plugin manifest's vocabulary.
ENGINE_CAPABILITIES = frozenset(plugin_kinds.CAPABILITIES)

RITUAL_SCHEDULES = frozenset({"daily", "weekly", "on-merge"})
VERIFY_CHECKS = frozenset(
    {"variables", "connections", "materials", "binaries", "capabilities", "instructions"}
)

RUNBOOK_TRIGGERS = frozenset({"operator", "ritual", "mission"})
RUNBOOK_CAPS = {"iterations": (1, 100), "wall_clock_minutes": (1, 24 * 60)}

# --- flows ---------------------------------------------------------------------------------------

ACTOR_AGENT = "agent"
ACTOR_EXTERNAL = "external"
ACTOR_OPERATOR = "operator"
ACTOR_NONE = "none"
ACTOR_KINDS = frozenset({ACTOR_AGENT, ACTOR_EXTERNAL, ACTOR_OPERATOR, ACTOR_NONE})
MEMORY_MODES = frozenset({"none", "read", "read-write"})
DISTINCT_CONSTRAINTS = frozenset({"session", "engine", "model"})

#: STEP OUTPUTS (#1096 §5): the closed, per-probe-kind set of observed facts a step may expose.
#: A step may declare only a slot one of its own checklist's probe kinds produces.
OUTPUT_SLOTS: dict[str, frozenset[str]] = {
    "forge_pr": frozenset({"pr_number", "head_branch", "head_sha"}),
    "git_local": frozenset({"branch", "head_sha"}),
}
assert set(OUTPUT_SLOTS) <= missions.PROBE_KINDS
#: What each slot IS, so a reference can only land in an argument that means the same thing.
SLOT_TYPES: dict[str, str] = {
    "pr_number": "pr_number",
    "head_branch": "branch",
    "head_sha": "sha",
    "branch": "branch",
}
assert {s for slots in OUTPUT_SLOTS.values() for s in slots} == set(SLOT_TYPES)
#: Which probe ARGUMENT accepts which slot type. Default-deny: an argument not named here takes no
#: step reference at all. `repo` and `url` are deliberately absent — they carry the AUTHORITY
#: (which repository, which host), and that only ever comes from an operator-reviewed variable
#: (a format-2 default only once individually confirmed)
#: (#1096 §5, "the bridge"); an observation may only narrow a target within it. No current
#: argument takes a `pr_number`, so a `pr_number` reference is refused wherever it appears until a
#: probe argument exists that means one.
ARG_SLOT_TYPES: dict[str, frozenset[str]] = {
    "branch": frozenset({"branch"}),
    "expect": frozenset({"sha"}),
}

#: The arguments a LITERAL may fill. Everything else is target-bearing and takes only a reference
#: (#1190 round 1: "a literal in a target-bearing argument (repo, url, branch) is refused").
#: Default-deny again: a probe argument added to `missions.PROBE_ARG_SCHEMA` later is
#: target-bearing until someone deliberately lists it here. Expectations (`expect_status`,
#: `expect`) describe what a good answer looks like; they never choose what is fetched.
LITERAL_ARGS = frozenset({"expect_status", "expect"})

#: The JSON type each argument contract expects, derived from the contract function itself so a
#: new contract in `missions` cannot be typed by guess here.
ARG_TYPE_BY_CONTRACT: dict[object, str] = {
    missions._arg_text: "text",
    missions._arg_url: "url",
    missions._arg_status: "int",
}
assert all(
    fn in ARG_TYPE_BY_CONTRACT
    for spec in missions.PROBE_ARG_SCHEMA.values()
    for (_req, fn) in spec.values()
), "every probe argument contract must have a playbook argument type"
#: The variable types that fit each argument type. `bool` fits no current argument, and an `int`
#: argument takes only an `int` variable — never a `bool`, which Python's `isinstance` would let
#: through (the termSize / `_arg_status` trap). A `secret` variable fits NO argument: a probe
#: target is shown, stored and fetched, and a secret is none of those (#1096 §3).
VAR_TYPES_FOR_ARG: dict[str, frozenset[str]] = {
    "text": frozenset({"text", "enum"}),
    "url": frozenset({"url"}),
    "int": frozenset({"int"}),
    "bool": frozenset({"bool"}),
}

# --- references ----------------------------------------------------------------------------------

#: A variable reference, spelled exactly as the template renderer spells it — a token the renderer
#: would not substitute is literal text, and treating it otherwise would make validation disagree
#: with rendering.
VAR_TOKEN_RE = template_send.FIELD_TOKEN_RE
STEP_TOKEN_RE = re.compile(r"\{\{steps\.([a-z][a-z0-9_-]{0,31})\.([a-z][a-z0-9_]{0,31})\}\}")
