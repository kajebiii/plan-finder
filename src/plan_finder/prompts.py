from __future__ import annotations

from .models import RejectionRecord

_SYSTEM_WRAPPER = """\
You are a senior software engineer reviewing a codebase for improvements.
Analyze the codebase in the current working directory.

USER'S REQUEST:
{user_prompt}

IMPORTANT RULES:
- Find exactly ONE improvement matching the user's request. Do not list multiple.
- Be specific: name exact files, functions, line ranges.
- Provide concrete implementation steps, not vague suggestions.
- If you genuinely cannot find any more improvements, set found_nothing to true.
{rejection_context}
"""

_REJECTION_CONTEXT_TEMPLATE = """
PREVIOUSLY REJECTED PLANS (do NOT suggest these or similar ideas again):
{rejections}
"""


MAX_REJECTIONS_IN_PROMPT = 50


def build_prompt(
    user_prompt: str,
    rejected_plans: list[RejectionRecord],
) -> str:
    """Wrap user prompt with system instructions and rejection context."""
    if rejected_plans:
        recent = rejected_plans[-MAX_REJECTIONS_IN_PROMPT:]
        lines = []
        if len(rejected_plans) > MAX_REJECTIONS_IN_PROMPT:
            lines.append(
                f"  (showing {MAX_REJECTIONS_IN_PROMPT} most recent "
                f"of {len(rejected_plans)} total)"
            )
        for i, r in enumerate(recent, 1):
            lines.append(f"  {i}. [{r.category}] {r.title}")
        rejection_text = _REJECTION_CONTEXT_TEMPLATE.format(
            rejections="\n".join(lines)
        )
    else:
        rejection_text = ""

    return _SYSTEM_WRAPPER.format(
        user_prompt=user_prompt,
        rejection_context=rejection_text,
    )
