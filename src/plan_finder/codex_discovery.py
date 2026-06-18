"""Codex CLI backend.

Mirrors the interface of :func:`plan_finder.discovery.discover_plan` but drives
the OpenAI Codex CLI (`codex exec`) instead of the Claude Agent SDK.

Codex is run non-interactively in a read-only sandbox. The model's final answer
is constrained to the DiscoveredPlan schema via `--output-schema`, and the JSONL
event stream (`--json`) is parsed for the session id, token usage, and live tool
activity. Codex is subscription-based, so no USD cost is reported (cost_usd=0.0).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .errors import RateLimitError
from .models import DiscoveredPlan


@dataclass
class DiscoveryResult:
    plan: DiscoveredPlan | None
    cost_usd: float
    total_tokens: int
    session_id: str | None
    model: str | None = None
    num_turns: int = 0


QUERY_TIMEOUT_SECONDS = 30 * 60  # 30 minutes per query

_PLAN_PROMPT_SUFFIX = (
    "\n\nReturn ONLY the structured plan as your final message, matching the "
    "provided output schema exactly."
)

_USAGE_LIMIT_MARKERS = (
    "usage limit",
    "hit your limit",
    "rate limit",
    "rate_limit",
    "quota",
)


def _build_codex_schema() -> dict:
    """Produce a strict-mode JSON schema Codex/OpenAI will accept.

    Inlines $defs (enum refs), strips keywords disallowed in strict mode
    (`title`, `default`), and marks every object's properties required with
    `additionalProperties: false`.
    """
    raw = DiscoveredPlan.model_json_schema()
    defs = raw.get("$defs", {})

    def resolve(node: object) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                target = copy.deepcopy(defs.get(name, {}))
                for k, v in node.items():
                    if k != "$ref":
                        target.setdefault(k, v)
                return resolve(target)
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    schema = resolve(raw)

    def strictify(node: object) -> None:
        if isinstance(node, dict):
            # `title`/`default` are disallowed in strict mode, but a field may
            # legitimately be *named* "title" — only strip schema keywords, never
            # keys inside the `properties` mapping.
            node.pop("title", None)
            node.pop("default", None)
            props = node.get("properties")
            if node.get("type") == "object" and isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for sub in value.values():
                        strictify(sub)
                else:
                    strictify(value)
        elif isinstance(node, list):
            for v in node:
                strictify(v)

    strictify(schema)
    return schema  # type: ignore[return-value]


def _parse_retry_at(message: str | None) -> datetime | None:
    """Parse a reset time from a Codex usage-limit message.

    e.g. "...try again at 7:45 PM." -> today/tomorrow 19:45 local.
    """
    if not message:
        return None
    m = re.search(
        r"try again at (\d{1,2})(?::(\d{2}))?\s*(AM|PM)", message, re.IGNORECASE
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3).upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _is_usage_limit(message: str | None) -> bool:
    if not message:
        return False
    lower = message.lower()
    return any(marker in lower for marker in _USAGE_LIMIT_MARKERS)


_SHELL_WRAPPER_RE = re.compile(r"^\S*/(?:zsh|bash|sh) -l?c (.*)$", re.DOTALL)


def _strip_shell_wrapper(cmd: str) -> str:
    """Codex shell commands arrive as `/bin/zsh -lc "<inner>"`; show the inner."""
    m = _SHELL_WRAPPER_RE.match(cmd)
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]:
        inner = inner[1:-1]
    return inner


def _summarize_item(item: dict) -> str:
    """Short human-readable summary of a Codex item for live activity."""
    kind = item.get("type")
    if kind == "command_execution":
        cmd = _strip_shell_wrapper(item.get("command", "")).replace("\n", " ")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"$ {cmd}"
    if kind == "file_change":
        return "Editing files"
    if kind == "mcp_tool_call":
        return f"{item.get('server', 'mcp')}.{item.get('tool', '')}"
    if kind == "web_search":
        return f"Web search: {item.get('query', '')[:50]}"
    if kind == "reasoning":
        return "Reasoning..."
    return str(kind or "working...")


def _extract_json(text: str) -> str:
    """Strip optional markdown fences around a JSON object."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    return s


def _build_args(
    *,
    schema_path: str,
    last_path: str,
    resume_session_id: str | None,
    model: str | None,
) -> list[str]:
    common = [
        "--json",
        "--skip-git-repo-check",
        "--output-schema",
        schema_path,
        "--output-last-message",
        last_path,
    ]
    if model:
        common += ["-m", model]

    if resume_session_id:
        # `codex exec resume` has no --sandbox/-C flags; enforce read-only via
        # config override and rely on the subprocess cwd for the working root.
        return [
            "exec",
            "resume",
            resume_session_id,
            "-c",
            'sandbox_mode="read-only"',
            *common,
            "-",
        ]
    return ["exec", "--sandbox", "read-only", *common, "-"]


async def discover_plan(
    prompt: str,
    cwd: str | None = None,
    resume_session_id: str | None = None,
    on_activity: Callable[[str], None] | None = None,
    model: str | None = None,
    max_turns: int = 80,  # accepted for interface parity; Codex self-manages turns
    effort: str | None = None,  # accepted for interface parity; Codex sets reasoning via its own config
) -> DiscoveryResult:
    """Run a single Codex query to discover one improvement plan."""
    import os

    target_dir = cwd or os.getcwd()

    schema = _build_codex_schema()
    with tempfile.TemporaryDirectory(prefix="plan-finder-codex-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        last_path = Path(tmp) / "last.txt"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        args = _build_args(
            schema_path=str(schema_path),
            last_path=str(last_path),
            resume_session_id=resume_session_id,
            model=model,
        )

        full_prompt = prompt + _PLAN_PROMPT_SUFFIX

        proc = await asyncio.create_subprocess_exec(
            "codex",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=target_dir,
        )

        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(full_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        session_id: str | None = resume_session_id
        usage: dict = {}
        last_agent_text: str | None = None
        error_message: str | None = None
        tool_count = 0

        async def _consume() -> None:
            nonlocal session_id, usage, last_agent_text, error_message, tool_count
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "thread.started":
                    session_id = ev.get("thread_id") or session_id
                elif etype == "turn.completed":
                    usage = ev.get("usage") or {}
                elif etype == "error":
                    error_message = ev.get("message") or error_message
                elif etype == "turn.failed":
                    err = ev.get("error") or {}
                    error_message = err.get("message") or error_message
                elif etype == "item.completed":
                    item = ev.get("item") or {}
                    if item.get("type") == "agent_message":
                        last_agent_text = item.get("text", last_agent_text)
                elif etype == "item.started":
                    item = ev.get("item") or {}
                    itype = item.get("type")
                    if itype != "agent_message":
                        if itype == "command_execution":
                            tool_count += 1
                        if on_activity:
                            on_activity(_summarize_item(item))

        try:
            await asyncio.wait_for(_consume(), timeout=QUERY_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise

        stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        returncode = await proc.wait()

        file_text = (
            last_path.read_text(encoding="utf-8") if last_path.exists() else None
        )

    # Surface provider errors so the engine can react (e.g. wait for reset).
    if error_message:
        if _is_usage_limit(error_message):
            raise RateLimitError(error_message, retry_at=_parse_retry_at(error_message))
        raise RuntimeError(f"codex error: {error_message}")
    if returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        detail = stderr_text[-300:] if stderr_text else f"exit code {returncode}"
        if _is_usage_limit(detail):
            raise RateLimitError(detail, retry_at=_parse_retry_at(detail))
        raise RuntimeError(f"codex exited with code {returncode}: {detail}")

    plan: DiscoveredPlan | None = None
    text = last_agent_text or file_text
    if text:
        try:
            plan = DiscoveredPlan.model_validate_json(_extract_json(text))
        except Exception:
            plan = None

    total_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))

    return DiscoveryResult(
        plan=plan,
        cost_usd=0.0,
        total_tokens=total_tokens,
        session_id=session_id,
        model=model,
        num_turns=tool_count,
    )
