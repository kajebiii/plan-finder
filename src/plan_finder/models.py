from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EffortLevel(str, Enum):
    trivial = "trivial"
    small = "small"
    medium = "medium"
    large = "large"
    epic = "epic"


class PlanCategory(str, Enum):
    bug_fix = "bug_fix"
    refactoring = "refactoring"
    performance = "performance"
    security = "security"
    code_quality = "code_quality"
    documentation = "documentation"
    testing = "testing"
    architecture = "architecture"
    dependency = "dependency"
    feature = "feature"
    other = "other"


class DiscoveredPlan(BaseModel):
    """Structured output schema returned by Claude."""

    title: str = Field(description="Short descriptive title for the improvement plan")
    category: PlanCategory = Field(description="Category of the improvement")
    description: str = Field(
        description="Detailed description of the improvement opportunity"
    )
    rationale: str = Field(description="Why this improvement matters")
    files_affected: list[str] = Field(
        description="List of file paths that would be modified"
    )
    estimated_effort: EffortLevel = Field(
        description="Estimated effort level to implement"
    )
    implementation_steps: list[str] = Field(
        description="Ordered list of concrete implementation steps"
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Potential risks or concerns with this change",
    )
    priority: int = Field(
        ge=1, le=5, description="Priority from 1 (highest) to 5 (lowest)"
    )
    found_nothing: bool = Field(
        default=False,
        description="Set to true if no further improvements were found",
    )


class RejectionRecord(BaseModel):
    """A single rejected plan entry stored in state."""

    title: str
    category: str
    description_summary: str
    rejected_at: datetime
    reason: str = ""


class PlanFinderState(BaseModel):
    """Persisted state across runs."""

    rejected_plans: list[RejectionRecord] = Field(default_factory=list)
    total_approved: int = 0
    total_rejected: int = 0
    last_run: datetime | None = None
