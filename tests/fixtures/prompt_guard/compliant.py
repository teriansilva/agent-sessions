"""Fixture: the shapes the guard must ACCEPT — content always from the registry accessor."""

from agent_sessions import prompts, pulse_chat


async def dict_literal(client):
    return await client.complete_json(
        [
            {"role": "system", "content": prompts.effective("session_recap")},
            {"role": "user", "content": "…"},
        ]
    )


async def sanitized_spread(client):
    """Replayed turns are fine when the pinned role gate is called AT the sink, qualified by
    its owning module so the symbol is unambiguous."""
    return await client.complete_json(
        [
            {"role": "system", "content": prompts.effective("chat_route")},
            *pulse_chat.bound_history([]),
        ]
    )


async def sdk_system_kwarg(client):
    return await client.responses.create(model="m", system=prompts.effective("chat_route"))


async def dict_call(client):
    return await client.complete_json(
        [dict(role="system", content=prompts.effective("handoff_brief"))]  # noqa: C408
    )
