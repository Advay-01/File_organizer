"""Data shapes shared across the organizer."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.safety import FileSnapshot


class FileInfo(BaseModel):
    """Local facts about one file plus a short content snippet."""

    path: str
    filename: str
    extension: str
    size_kb: float
    date_created: str
    snippet: str = ""
    warnings: List[str] = Field(default_factory=list)
    source_snapshot: FileSnapshot | None = Field(default=None, exclude=True)


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
    """Validated decision fields; ignore optional model-added annotations."""

    # Providers can add explanatory metadata such as subcategory_evidence.
    # Only declared fields reach the planner; required fields and enums still
    # validate normally, and file IDs are checked against the current batch.
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=0)
    category: str = Field(min_length=1, max_length=80)
    subcategory: str = Field(min_length=1, max_length=80)
    confidence: Literal["high", "medium", "low", "insufficient"]
    reasoning: str = Field(default="", max_length=1000)
    evidence_quote: str = Field(default="", max_length=500)
    filename_conflict: Literal["none", "generic", "misleading", "extension_mismatch"] = "none"
    suggested_name: str = Field(default="", max_length=200)
    name_evidence: str = Field(default="", max_length=500)

    @field_validator("subcategory", mode="before")
    @classmethod
    def normalize_blank_subcategory(cls, value):
        # The planner already uses General for an explicitly blank subfolder.
        # Keep missing fields, nulls, and other invalid types subject to validation.
        if isinstance(value, str) and not value.strip():
            return "General"
        return value


class ContentIntelligenceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    files: List[ContentIntelligenceAction]
