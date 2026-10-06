"""Render the workspace flow reference reviewed before deployment (#1096 §11).

This consumes validated bundle data and the review's resolved text, assignments and targets.
It reads no store or roster, runs nothing and writes nothing. The generated material joins the
same destination snapshot and review digest as declared materials; the lifecycle owns its writes.
"""

from __future__ import annotations

import html
import json
import re

from ..template_send import FIELD_TOKEN_RE, substitute
from . import materials, schema

PATH = "docs/playbook.md"


class _Lines(list[str]):
    """Bound the accumulated document while rendering, before visiting another step."""

    def __init__(self, lines: list[str]):
        super().__init__()
        self.size = 0
        self.extend(lines)

    def append(self, line: str) -> None:
        self.size += len(line.encode("utf-8")) + 1
        if self.size > schema.MAX_FILE_BYTES:
            raise materials.MaterialError(
                f"{PATH}: generated reference exceeds the file size limit"
            )
        super().append(line)

    def extend(self, lines: list[str]) -> None:
        for line in lines:
            self.append(line)


def _inline(text: str) -> str:
    text = " ".join(text.splitlines())
    return re.sub(r"([\\`*_\[\]#|])", r"\\\1", html.escape(text, quote=False))


def _block(text: str) -> list[str]:
    # A brief may itself show fenced code. It must not close the surrounding reference block.
    longest = max((len(m[0]) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence, ""]


def render(
    bundle: dict, values: dict[str, str], assignments: list[dict], targets: list[dict]
) -> materials.Material | None:
    """Return a managed reference, or None for a bundle without flows. Never infer execution."""
    if not bundle["flows"]:
        return None
    fields = {v["name"]: v for v in bundle["variables"]}

    def expand(text: str) -> str:
        for match in FIELD_TOKEN_RE.finditer(text):
            name = match[1]
            if name not in fields or fields[name]["kind"] != "text":
                raise materials.MaterialError(f"{PATH}: {name} is not a declared text variable")
            if not isinstance(values.get(name), str):
                raise materials.MaterialError(f"{PATH}: {name} needs a resolved text value")
        return substitute(text, list(fields.values()), values)

    assigned = {row["step"]: row for row in assignments}
    target_by_id = {row["id"]: row for row in targets}
    identity = bundle["identity"]
    lines = _Lines(
        [
            "# " + _inline(expand(identity["name"])),
            "",
            f"Playbook `{identity['id']}`, version `{identity['version']}`.",
            "",
            "This is a workflow reference. Current mission evidence, operator decisions and "
            "execution authority are checked by BattleLab. Completing an agent turn does not "
            "complete a step.",
            "",
        ]
    )
    for fid, flow in bundle["flows"].items():
        lines.extend(["## " + _inline(expand(flow["title"])), "", f"Flow: `{fid}`.", ""])
        if flow["description"]:
            lines.extend(_block(expand(flow["description"])))
        for step in flow["steps"]:
            sid = step["id"]
            lines.extend([f"### {sid}: " + _inline(expand(step["title"])), ""])
            actor = step["actor"]
            if actor["kind"] == schema.ACTOR_AGENT:
                row = assigned.get(f"{fid}:{sid}")
                selection = row["assignment"] if row is not None else None
                if selection is None:
                    reason = row["reason"] if row is not None else None
                    reason = reason or "assignment not resolved"
                    lines.append("Actor: agent, unassigned (" + _inline(reason) + ").")
                else:
                    requested = row["requested"]["model"]
                    resolved = (
                        ""
                        if selection["model"] == requested
                        else f", resolved to `{selection['model']}`"
                    )
                    lines.append(
                        f"Actor: agent `{selection['engine']}`, requested model "
                        f"`{requested}`{resolved}. The runtime must verify the actual model."
                    )
            elif actor["kind"] == schema.ACTOR_EXTERNAL:
                lines.append("Actor: external — " + _inline(expand(actor["label"])) + ".")
            elif actor["kind"] == schema.ACTOR_OPERATOR:
                lines.append("Actor: operator.")
            else:
                lines.append("Actor: none; this step dispatches no agent.")
            if step["after"]:
                lines.append(
                    "Prerequisites (all required): "
                    + ", ".join(f"`{parent}`" for parent in step["after"])
                    + "."
                )
            else:
                lines.append("Prerequisites: none.")
            for constraint in step["distinct_from"]:
                lines.append(
                    f"Independent `{constraint['constraint']}` required from step "
                    f"`{constraint['step']}`."
                )
            lines.append("")
            if step["brief"]:
                lines.extend(_block(expand(step["brief"])))
            if not step["checklist"]:
                lines.extend(["No checklist evidence is declared for this step.", ""])
                if actor["kind"] == schema.ACTOR_NONE:
                    lines.extend(["This is a note; it is never a prerequisite.", ""])
            for item in step["checklist"]:
                requirement = "required" if item["required"] else "optional"
                lines.extend(
                    [
                        f"- `{item['key']}` ({requirement}, `{item['probe']}`): "
                        + _inline(expand(item["title"])),
                        "",
                    ]
                )
                target = target_by_id.get(f"flow:{fid}:{sid}:{item['key']}")
                if target is None:
                    raise materials.MaterialError(f"{PATH}: a checklist target was not resolved")
                if target["args"]:
                    lines.append("Reviewed arguments:")
                    lines.extend(_block(json.dumps(target["args"], sort_keys=True, indent=2)))
                if target["pending"]:
                    lines.append("Awaiting observed outputs from the current step episodes:")
                    lines.extend(_block(json.dumps(target["pending"], sort_keys=True, indent=2)))
                if target["requires_confirmation"]:
                    lines.extend(
                        ["This default-derived target requires individual confirmation.", ""]
                    )
            if step["outputs"]:
                lines.extend(
                    [
                        "Observed outputs: "
                        + ", ".join(f"`{slot}`" for slot in step["outputs"])
                        + ". Only current-episode observations may supply them.",
                        "",
                    ]
                )
            rework = step["rework"]
            if rework is not None:
                lines.extend(
                    [
                        f"Rework: a failed `{rework['when']}` result reopens `{rework['to']}` "
                        f"for at most {rework['max_rounds']} rounds on this edge. "
                        "After the limit, ask the operator. Repeated evidence consumes no "
                        "additional round; a reopened step needs fresh evidence.",
                        "",
                    ]
                )
    for runbook in bundle["runbooks"].values():
        if runbook["bail"]:
            lines.extend(
                [
                    "## Bail conditions: " + _inline(expand(runbook["title"])),
                    "",
                    f"Applies only when this runbook runs (trigger: `{runbook['trigger']}`).",
                    "",
                ]
            )
            for condition in runbook["bail"]:
                lines.extend(_block(expand(condition)))
    data = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    return materials.Material(PATH, "managed", data=data)


def include(
    rendered: list[materials.Material], document: materials.Material | None
) -> list[materials.Material]:
    """Combine declared and generated materials without adopting a bundle-owned path."""
    if document is None:
        return rendered
    for material in rendered:
        if (
            material.path == PATH
            or PATH.startswith(material.path + "/")
            or material.path.startswith(PATH + "/")
        ):
            raise materials.MaterialError(f"{PATH} is reserved for the generated flow reference")
    result = [*rendered, document]
    if len(result) > schema.MAX_MATERIALS or sum(len(m.data or b"") for m in result) > (
        schema.MAX_TOTAL_BYTES
    ):
        raise materials.MaterialError("declared and generated materials exceed the bundle limit")
    return result
