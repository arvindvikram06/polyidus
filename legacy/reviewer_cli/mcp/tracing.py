"""Wrap an MCP tool so the tracer records it against the calling specialist.

Shared by every MCP backend. Tracing deliberately stays on this side of the
wire: ``tracer.tool_call`` fires inside the calling run's context, so each call
is attributed to the specialist that made it. The server has no idea which
specialist is asking, and does not need to.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from reviewer.core.tracer import tracer


def as_text(result: Any) -> str:
    """Flatten a tool result into a line the tracer can summarise."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(
            block.get("text", "") for block in result if isinstance(block, dict)
        )
    return str(result)


def _fingerprint(name: str, args: dict[str, Any]) -> str:
    return name + "|" + json.dumps(args, sort_keys=True, default=str)


def _schema_requiring(schema: Any, required: tuple[str, ...]) -> Any:
    """``schema`` with ``required`` promoted to mandatory arguments.

    GitHub declares `get_file_contents` as requiring only owner and repo, with
    `path` optional — call it without one and you get a directory listing,
    which is a *successful* response. So a model that omits `path` gets no
    signal that it did anything wrong.

    Returning a correction as tool output was not enough. Measured on a real
    run: 18 calls to `get_file_contents`, 17 of them the identical no-path
    call. One specialist made it six times and read nothing at all, and
    another made it nine more times *after* being told it had already made the
    same invalid call twice. Text in a tool result is advice the model can
    decline to take.

    Marking the argument required in the schema is not advice. The function
    calling layer will not emit a call that omits it, so the invalid call
    becomes unrepresentable rather than merely discouraged.
    """
    if not required:
        return schema

    if hasattr(schema, "model_json_schema"):
        json_schema = schema.model_json_schema()
    elif isinstance(schema, dict):
        json_schema = dict(schema)
    else:  # an args_schema shape we do not recognise; leave it alone
        return schema

    properties = json_schema.get("properties") or {}
    # Only promote arguments the tool actually declares. Demanding one the
    # server does not accept would break every call instead of fixing it.
    promote = [name for name in required if name in properties]
    if not promote:
        return schema

    return {
        **json_schema,
        "required": sorted(set(json_schema.get("required") or []) | set(promote)),
    }


def traced(
    tool: BaseTool,
    before: Callable[[], Awaitable[None]] | None = None,
    overrides: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    annotate: Callable[[str, Any], str | None] | None = None,
    description: str | None = None,
) -> BaseTool:
    """Return ``tool`` with tracing, an optional pre-call hook, and fixed args.

    ``before`` is where a rate limiter goes: it runs after the agent has decided
    to make the call but before the request leaves, so a throttled call still
    shows up in the trace in the order the agent asked for it.

    ``overrides`` pin argument values the caller must not choose — the target
    repository, in practice. They are applied last, so a model supplying
    something else is corrected rather than obeyed. A new tool is returned
    rather than the original mutated: LangChain tools are Pydantic models and
    refuse attribute assignment.

    ``required`` names arguments whose absence is answered with a correction
    instead of a request. Two failure modes killed real runs, and neither
    produced an error the model could learn from: omitting `path` returns a
    directory listing, and repeating an identical call returns the identical
    result. Both are answered here, in the model's own feedback channel.
    """
    seen: dict[str, set[str]] = {}

    async def call(**kwargs: Any) -> Any:
        # Unset optionals are dropped so the server applies its own defaults.
        args = {key: value for key, value in kwargs.items() if value is not None}
        if overrides:
            args.update(overrides)

        # Repeat detection comes first so it also covers calls this wrapper
        # rejects. Checking it second let a model repeat the *same rejected*
        # call indefinitely: the rejection returned before anything was
        # recorded, so the identical retry looked new every time.
        run_id = getattr(getattr(tracer, "_run", None), "id", "-")
        key = _fingerprint(tool.name, args)
        repeated = key in seen.setdefault(run_id, set())
        seen[run_id].add(key)

        missing = [name for name in required if not args.get(name)]
        if missing:
            message = (
                f"error: {tool.name} requires {', '.join(missing)}. "
                f"Calling it without {missing[0]} returns a directory listing, not a file. "
                "Pass a repository-relative path to one of the files in scope"
                + (
                    ". You have now made this same invalid call twice — stop "
                    "calling this tool and report the findings you already have."
                    if repeated
                    else "."
                )
            )
            tracer.tool_call(tool.name, args, message)
            return message

        if repeated:
            message = (
                f"error: you already called {tool.name} with these exact arguments "
                "in this run and the result has not changed. Ask a different "
                "question or produce your findings now."
            )
            tracer.tool_call(tool.name, args, message)
            return message

        if before is not None:
            await before()
        try:
            result = await tool.ainvoke(args)
        except Exception as exc:
            # Transport failures are not something an agent can act on, so they
            # propagate — but the trace should still show the attempt.
            tracer.tool_call(tool.name, args, f"error: {type(exc).__name__}: {exc}")
            raise
        tracer.tool_call(tool.name, args, as_text(result))
        # A result that is technically successful but useless gets a note the
        # model can act on, rather than leaving it to infer why it got nothing.
        if annotate is not None:
            explanation = annotate(tool.name, result)
            if explanation:
                return f"{as_text(result)}\n\n{explanation}"
        return result

    return StructuredTool(
        name=tool.name,
        # GitHub writes its descriptions for a general-purpose assistant. A
        # specialist reviewing one diff needs different advice, and the
        # description is the only place the model reads before choosing a tool.
        description=description or tool.description,
        # Required arguments are enforced in the schema, not just checked on
        # arrival — see `_schema_requiring`. The runtime check above stays as a
        # backstop for providers that do not honour `required`.
        args_schema=_schema_requiring(tool.args_schema, required),
        coroutine=call,
    )
