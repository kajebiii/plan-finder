from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    query,
)

from .models import DiscoveredPlan


@dataclass
class DiscoveryResult:
    plan: DiscoveredPlan | None
    cost_usd: float
    total_tokens: int
    session_id: str | None
    model: str | None = None
    num_turns: int = 0


QUERY_TIMEOUT_SECONDS = 45 * 60  # 45 minutes per query


async def discover_plan(
    prompt: str,
    cwd: str | None = None,
    resume_session_id: str | None = None,
    on_activity: Callable[[str], None] | None = None,
    model: str | None = None,
    max_turns: int = 80,
    effort: str | None = None,
) -> DiscoveryResult:
    """Run a single Claude query to discover one improvement plan."""
    import asyncio

    target_dir = cwd or os.getcwd()

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Glob", "Grep", "WebSearch", "Bash"],
        permission_mode="bypassPermissions",
        cwd=target_dir,
        max_turns=max_turns,
        output_format={
            "type": "json_schema",
            "schema": DiscoveredPlan.model_json_schema(),
        },
        system_prompt=(
            "You are in READ-ONLY mode. You may only use Read, Glob, Grep, "
            "WebSearch, and Bash (read-only commands only like ls, git log, wc, etc.). "
            "Do NOT modify any files. Your goal is to analyze the codebase "
            "and produce a structured improvement plan."
        ),
    )

    if model:
        options.model = model

    if effort:
        # Claude CLI accepts `--effort <level>` (low/medium/high/xhigh/max).
        # claude-agent-sdk passes extra_args through verbatim, so we need a
        # plain string here. Unwrap Enum members defensively in case a caller
        # passes one in directly (e.g. EffortLevel.max from cli.py).
        effort_str = getattr(effort, "value", None) or str(effort)
        options.extra_args = {**(options.extra_args or {}), "effort": effort_str}

    if resume_session_id:
        options.resume = resume_session_id

    async def _run_query() -> DiscoveryResult:
        plan: DiscoveredPlan | None = None
        cost: float = 0.0
        tokens: int = 0
        session_id: str | None = None
        _model: str | None = None
        turns: int = 0

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                if _model is None:
                    _model = message.model
                has_tool_use = False
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        has_tool_use = True
                        if on_activity:
                            detail = _summarize_tool(block.name, block.input)
                            on_activity(detail)
                if has_tool_use:
                    turns += 1
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                session_id = message.session_id
                if message.usage:
                    u = message.usage
                    tokens = (
                        u.get("input_tokens", 0)
                        + u.get("output_tokens", 0)
                        + u.get("cache_read_input_tokens", 0)
                        + u.get("cache_creation_input_tokens", 0)
                    )
                if message.is_error:
                    # CLI v2.1.110+ emits is_error=true with subtype="success"
                    # and api_error_status set (e.g. 429 rate limit, 500/502/
                    # 503/529 transient) when the underlying Anthropic API
                    # call failed. The SDK's ProcessError wrapper falls back
                    # to the subtype string when errors[] is empty, so the
                    # engine would only see the unhelpful literal "success".
                    # Pull every diagnostically useful field off ResultMessage
                    # so the engine logs a single line a human can act on:
                    #   - api_error_status: HTTP code (529/429/5xx)
                    #   - subtype: error category (success/error_max_turns/...)
                    #   - stop_reason: where the model halted (api_error/
                    #     refusal/max_tokens/end_turn/tool_use)
                    #   - num_turns: 1st turn rejection vs deep-context blow-up
                    #   - duration_api_ms: fast reject vs long timeout
                    #   - session_id: short prefix for resume tracking
                    #   - errors[]: raw API error body (when present)
                    raise RuntimeError(_format_result_error(message))
                if message.subtype == "success" and message.structured_output:
                    plan = DiscoveredPlan.model_validate(message.structured_output)

        return DiscoveryResult(
            plan=plan, cost_usd=cost, total_tokens=tokens, session_id=session_id,
            model=_model, num_turns=turns,
        )

    return await asyncio.wait_for(_run_query(), timeout=QUERY_TIMEOUT_SECONDS)


def _format_result_error(msg: ResultMessage) -> str:
    """Render a ResultMessage with is_error=true as a single diagnostic line.

    Keep keys cheap to grep and values compact — the engine logs the first
    ~120 chars, so put the most decisive fields (HTTP status, subtype,
    stop_reason) first.
    """
    parts: list[str] = []
    if msg.api_error_status:
        parts.append(f"HTTP {msg.api_error_status}")
    parts.append(f"subtype={msg.subtype!r}")
    if msg.stop_reason:
        parts.append(f"stop_reason={msg.stop_reason!r}")
    parts.append(f"turn={msg.num_turns}")
    parts.append(f"api_ms={msg.duration_api_ms}")
    if msg.session_id:
        parts.append(f"session={msg.session_id[:8]}")
    if msg.errors:
        joined = "; ".join(msg.errors)[:200]
        parts.append(f"errors={joined!r}")
    return "Claude API call failed: " + " | ".join(parts)


def _summarize_tool(name: str, inp: dict) -> str:
    """Create a short human-readable summary of a tool call."""
    if name == "Read":
        path = inp.get("file_path", "")
        return f"Reading {_short_path(path)}"
    if name == "Glob":
        return f"Searching {inp.get('pattern', '')}"
    if name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        suffix = f" in {_short_path(path)}" if path else ""
        return f"Grep '{pattern}'{suffix}"
    if name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"$ {cmd}"
    return f"{name}(...)"


def _short_path(path: str) -> str:
    """Shorten a path to last 2 components."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 2:
        return "/".join(parts[-2:])
    return path
