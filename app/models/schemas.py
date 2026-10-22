"""Data shapes shared across the organizer."""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """Local facts about one file plus a short content snippet."""

    path: str
    filename: str
    extension: str
    size_kb: float
    date_created: str
    snippet: str = ""


class SortingAction(BaseModel):
    """One move decided by the LLM (validated before anything touches disk)."""

    path: str = Field(description="Original path, copied exactly from the input")
    folder: str = Field(description="Topic folder name, e.g. AI, DSA, Projects")
    new_name: str = Field(default="", description="Optional cleaner filename; empty = keep")
    reason: str = ""


class OrganizerResponse(BaseModel):
    actions: List[SortingAction]
