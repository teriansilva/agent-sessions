"""Fixture: every shape the guard must REJECT.

Fifteen ways a prompt can slip back out of the registry — a bare module constant, an f-string
assembled at the call, a variable built elsewhere, the editable text (which for a guarded
prompt is missing the clause `effective()` appends), a `dict(role=…)` call instead of a dict
literal, a role that is only readable at runtime, and a payload handed to the sink as a
variable the checker cannot read at all.
"""

from agent_sessions import prompts

SYSTEM = "You are a helpful assistant."
EXTRA = "Be brief."
ROLE = "system"
DICT_SYSTEM = "You are a helpful assistant, via dict()."


async def bare_constant(client):
    return await client.complete_json([{"role": "system", "content": SYSTEM}])


async def f_string(client):
    return await client.complete_json([{"role": "system", "content": f"{SYSTEM} {EXTRA}"}])


async def indirect_variable(client):
    text = SYSTEM + EXTRA
    return await client.complete_json([{"role": "system", "content": text}])


async def editable_not_effective(client):
    return await client.complete_json(
        [{"role": "system", "content": prompts.editable("chat_instruct")}]
    )


async def dict_call(client):
    """`dict(role=…)` is the same message; a literal-only checker cannot see it."""
    return await client.complete_json([dict(role="system", content=DICT_SYSTEM)])  # noqa: C408


async def role_in_a_variable(client):
    """The role is only knowable at runtime — the checker must not assume it is safe."""
    return await client.complete_json([{"role": ROLE, "content": SYSTEM}])


async def unsanitized_spread(client):
    """A spread that never went through the role gate. The turns are client-supplied, so this
    can carry a system message — and the file already has sanitized spreads, which is exactly
    the case a per-file allowlist would wave through."""
    history = [{"role": "system", "content": SYSTEM}]
    return await client.complete_json(
        [{"role": "system", "content": prompts.effective("chat_route")}, *history]
    )


def bound_history(_history):
    """A same-named local that gates NOTHING — the spoof a name-based check would accept."""
    return [{"role": ROLE, "content": SYSTEM}]


async def spoofed_sanitizer(client):
    """The spread calls something spelled `bound_history`, but not the pinned one."""
    return await client.complete_json(
        [{"role": "system", "content": prompts.effective("chat_route")}, *bound_history([])]
    )


async def sdk_system_kwarg(client):
    """The SDK shape: no message list at all, the prompt rides a `system=` keyword."""
    return await client.responses.create(model="m", system=SYSTEM, input="hi")


class pulse_chat:  # noqa: N801 — deliberately shadows the real module name
    """A local binding of the sanitizer MODULE name. Reads exactly like the approved call."""

    @staticmethod
    def bound_history(_history):
        return [{"role": ROLE, "content": SYSTEM}]


async def spoofed_sanitizer_module(client):
    return await client.complete_json(
        [
            {"role": "system", "content": prompts.effective("chat_route")},
            *pulse_chat.bound_history([]),
        ]
    )


async def direct_transport_post(client):
    """No known sink at all — the message goes straight onto an HTTP POST. The role is only
    knowable at runtime, so the message literal itself is the offence, wherever it travels."""
    return await client.post(
        "https://ai.example/v1/chat/completions",
        json={"model": "m", "messages": [{"role": ROLE, "content": SYSTEM}]},
    )


ROLE_FIELDS = {"role": ROLE}


async def indirect_role_map_direct_post(client):
    """The subtlest one: the message is assembled from a role MAP, so no literal carries both
    `role` and `content`, and it is posted straight at the endpoint so no enforced transport
    ever sees it. Nothing about the message is readable — the offence is building the request
    outside `review._post_chat` at all."""
    message = dict(ROLE_FIELDS, content=SYSTEM)
    return await client.post(
        "https://ai.example/v1/chat/completions",
        json={"model": "m", "messages": [message]},
    )


async def assembled_url_and_variable_body(client):
    """Nothing here is readable: the message comes from a role map, the body is a variable, and
    the URL is built from pieces. Reading payloads cannot catch this — but the POST itself is
    still a POST, and it is not one of the pinned transports."""
    message = dict(ROLE_FIELDS, content=SYSTEM)
    body = {"model": "m", "messages": [message]}
    endpoint = "/chat/" + "completions"
    return await client.post(endpoint, json=body)


async def generic_request_verb(client):
    """`client.request("POST", …)` is a POST that is not spelled `.post`. The inventory is
    about the capability, so the verb it happens to use does not decide whether it counts."""
    message = dict(ROLE_FIELDS, content=SYSTEM)
    return await client.request("POST", "/chat/" + "completions", json={"messages": [message]})


async def payload_assembled_elsewhere(client):
    """The whole list is built out of view. Fail closed: an unreadable payload is an offence."""
    messages = [{"role": "system", "content": SYSTEM}]
    return await client.complete_json(messages)
