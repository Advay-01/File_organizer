"""Bounded candidate hints and safe repeated-work reduction for the existing planner.

Semantic neighbours are hints for a per-file LLM decision, never assignments.
Only byte-identical, context-compatible files can reuse a validated decision.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from app.core.evidence import EvidenceBundle
from app.core.semantic_index import SemanticIndex

WORKLOAD_VERSION = 1
_MAX_BATCH_COMPARISONS = 256
_MAX_BATCH_HINTS = 64


def duplicate_key(file, evidence: EvidenceBundle, *, allow_rename: bool, instructions: list[str]) -> str | None:
    # Names and their evidence must be evaluated individually when renaming, and
    # explicit instructions may distinguish copies by name/path even without it.
    if (allow_rename or instructions or evidence.sensitive or not evidence.fingerprint
            or evidence.extraction_status != "sufficient" or len(evidence.content.strip()) < 160):
        return None
    return json.dumps([evidence.fingerprint, evidence.extension, evidence.detected_type,
                       os.path.normcase(os.path.abspath(Path(file.path).parent)), evidence.metadata],
                      sort_keys=True, ensure_ascii=False)


def reduce_duplicates(todo: list, *, allow_rename: bool, instructions: list[str], enabled: bool = True):
    representatives, aliases, seen = [], {}, {}
    for item in todo:
        file, evidence, _key = item
        key = duplicate_key(file, evidence, allow_rename=allow_rename, instructions=instructions) if enabled else None
        if key is not None and key in seen:
            aliases.setdefault(seen[key], []).append(item)
        else:
            representatives.append(item)
            if key is not None:
                seen[key] = file.path
    return representatives, aliases


def candidate_order(todo: list, index: SemanticIndex, ids: dict[str, int]):
    """Put candidate neighbours in the same LLM batch, with a bounded search budget."""
    remaining = {ids[file.path]: item for item in todo for file in [item[0]]}
    ordered, searches = [], 0
    max_searches = max(1, min(128, 50000 // max(1, len(index.vectors))))
    for item in todo:
        file_id = ids[item[0].path]
        if file_id not in remaining:
            continue
        ordered.append(remaining.pop(file_id))
        if file_id not in index.vectors or searches >= max_searches:
            continue
        searches += 1
        for neighbour, score in index.candidates(file_id, limit=8):
            if score >= 0.75 and neighbour in remaining:
                ordered.append(remaining.pop(neighbour))
    index.stats["semantic_candidate_lookups"] = searches
    return ordered


def batch_hints(batch: list, cards: list[dict], index: SemanticIndex, ids: dict[str, int], relationships: list) -> dict:
    """Reference only IDs supplied in this batch; retain each member's own evidence."""
    local = {ids[item[0].path]: number for number, item in enumerate(batch)}
    neighbours = []
    comparisons = 0
    for file_id, number in local.items():
        if comparisons >= _MAX_BATCH_COMPARISONS or len(neighbours) >= _MAX_BATCH_HINTS:
            break
        vector = index.vectors.get(file_id)
        if vector is None:
            continue
        # Compare only this batch, avoiding an all-pairs scan of the full folder.
        for other, other_number in local.items():
            if number >= other_number or other not in index.vectors:
                continue
            if comparisons >= _MAX_BATCH_COMPARISONS or len(neighbours) >= _MAX_BATCH_HINTS:
                break
            comparisons += 1
            score = math.fsum(a * b for a, b in zip(vector, index.vectors[other]))
            if score >= 0.75:
                neighbours.append({"a": number, "b": other_number, "similarity": round(min(1.0, score), 4)})
    explicit = []
    for edge in relationships:
        if edge.source_id not in local or edge.target_id not in local:
            continue
        explicit.append({"a": local[edge.source_id], "b": local[edge.target_id],
                         "kind": edge.kind, "strength": edge.strength})
        if len(explicit) >= _MAX_BATCH_HINTS:
            break
    return {"semantic_candidates": neighbours, "relationships": explicit}
