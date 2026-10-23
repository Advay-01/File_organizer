"""Data shapes shared across the organizer."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileInfo(BaseModel):
    """Local facts about one file plus a short content snippet."""

    path: str
    filename: str
    extension: str
    size_kb: float
    date_created: str
    snippet: str = ""
    warnings: List[str] = Field(default_factory=list)


class SortingAction(BaseModel):
    """One move decided by the LLM (validated before anything touches disk)."""

    path: str = Field(description="Original path, copied exactly from the input")
    target_path: str = Field(
        default="",
        description="Required two-tier destination, e.g. Physics/Lecture Notes",
    )
    reasoning: str = Field(default="", description="Short explanation of the grouping")
    folder: str = Field(default="", description="Legacy topic folder name, e.g. AI, DSA, Projects")
    subfolder: str = Field(
        default="",
        description="Optional one-level category inside folder, e.g. Notes or Exams",
    )
    new_name: str = Field(default="", description="Optional cleaner filename; empty = keep")
    reason: str = ""


class OrganizerResponse(BaseModel):
    actions: List[SortingAction]


class ContentIntelligenceAction(BaseModel):
    """Strict response item from the content-intelligence model."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    category: str = Field(min_length=1, max_length=80)
    subcategory: str = Field(min_length=1, max_length=80)
    confidence: Literal["high", "medium", "low", "insufficient"]
    reasoning: str = Field(default="", max_length=1000)
    evidence_quote: str = Field(default="", max_length=500)
    filename_conflict: Literal["none", "generic", "misleading", "extension_mismatch"] = "none"
    suggested_name: str = Field(default="", max_length=200)
    name_evidence: str = Field(default="", max_length=500)


class ContentIntelligenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: List[ContentIntelligenceAction]
