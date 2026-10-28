"""Conservative, typed relationships between files already selected for a preview.

Edges describe evidence; they never authorize a move or merge file identities.
The graph is ephemeral, and no referenced paths are opened or fetched here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from app.core.evidence import EvidenceBundle


@dataclass(frozen=True)
class FileRelationship:
    source_id: int
    target_id: int
    kind: str
    strength: str
    evidence: str


def _identifier(value: object) -> str:
    """IDs are opaque, case-sensitive metadata, not executable paths or URLs."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not 3 <= len(value) <= 256 or re.search(r"[\s\x00-\x1f<>\"']", value):
        return ""
    return value


def _path_key(path: Path) -> str:
    # Purely lexical: resolving paths could traverse a link to an unselected file.
    return os.path.normcase(os.path.abspath(path))


def _selected_reference(source: EvidenceBundle, reference: object, paths: dict[str, int]) -> int | None:
    if not isinstance(reference, str) or len(reference) > 2048:
        return None
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.query:
        return None
    value = unquote(parsed.path)
    if "\x00" in value or "\\" in value or Path(value).is_absolute():
        return None
    # A parent-relative reference is permitted only when it names an already
    # selected file. Matching this index performs no filesystem access.
    return paths.get(_path_key(Path(source.path).parent / value))


def relationship_groups(evidence_by_id: dict[int, EvidenceBundle]) -> list[FileRelationship]:
    """Return typed edges; weak/ascribed metadata never becomes a must-link rule.

    ``duplicate`` is verified by a complete content hash. ``derivation`` and
    ``dependency`` are metadata assertions. ``possible_version`` is a candidate
    supported by a shared document ID and distinct instance IDs. Names, authors,
    timestamps and file extensions alone deliberately create no edge.
    """
    files = {key: value for key, value in sorted(evidence_by_id.items()) if not value.sensitive}
    edges: dict[tuple[int, int, str], FileRelationship] = {}

    def add(source: int, target: int, kind: str, strength: str, reason: str) -> None:
        if source != target:
            edges.setdefault((source, target, kind), FileRelationship(source, target, kind, strength, reason))

    hashes: dict[str, list[int]] = defaultdict(list)
    document_ids: dict[str, list[int]] = defaultdict(list)
    instance_ids: dict[str, list[int]] = defaultdict(list)
    paths = {_path_key(Path(value.path)): key for key, value in files.items()}
    for key, value in files.items():
        if re.fullmatch(r"[0-9a-fA-F]{64}", value.fingerprint or ""):
            hashes[value.fingerprint.lower()].append(key)
        if value.metadata.get("xmp_relationship_conflict"):
            continue
        document_id = _identifier(value.metadata.get("xmp_document_id"))
        instance_id = _identifier(value.metadata.get("xmp_instance_id"))
        if document_id:
            document_ids[document_id].append(key)
        if instance_id:
            instance_ids[instance_id].append(key)

    # A star avoids quadratic work for a directory containing thousands of copies.
    for members in hashes.values():
        for key in members[1:]:
            add(key, members[0], "duplicate", "verified", "The complete SHA-256 content fingerprints match.")

    for key, value in files.items():
        metadata = value.metadata
        if metadata.get("xmp_relationship_conflict"):
            continue
        parent_document = _identifier(metadata.get("xmp_derived_from_document_id"))
        parent_instance = _identifier(metadata.get("xmp_derived_from_instance_id"))
        parents = instance_ids.get(parent_instance, []) if parent_instance else document_ids.get(parent_document, [])
        if len(parents) > 64:
            parents = []
        if parent_instance and parent_document:
            parents = [target for target in parents
                       if _identifier(files[target].metadata.get("xmp_document_id")) == parent_document]
        # An ambiguous ID must not invent a specific parent/version relationship.
        if len(parents) == 1:
            target = parents[0]
            source_instance = _identifier(metadata.get("xmp_instance_id"))
            if not parent_instance or not source_instance or source_instance != parent_instance:
                add(key, target, "derivation", "asserted", "Embedded XMP declares this selected file as a source.")

        original_id = _identifier(metadata.get("xmp_original_document_id"))
        if original_id and original_id != _identifier(metadata.get("xmp_document_id")):
            originals = document_ids.get(original_id, [])
            if len(originals) == 1:
                add(key, originals[0], "derivation", "asserted", "Embedded XMP declares this selected file as the original source.")

        document_id = _identifier(metadata.get("xmp_document_id"))
        instance_id = _identifier(metadata.get("xmp_instance_id"))
        if value.extension.casefold() == ".xmp" and document_id and instance_id:
            sidecar_path = Path(value.path)
            companions = []
            candidates = document_ids.get(document_id, [])
            for target in candidates if len(candidates) <= 64 else []:
                other = files[target]
                other_path = Path(other.path)
                if (
                    target != key and other.extension.casefold() != ".xmp"
                    and _identifier(other.metadata.get("xmp_instance_id")) == instance_id
                    and _path_key(other_path.parent) == _path_key(sidecar_path.parent)
                    and sidecar_path.stem.casefold() in {other_path.stem.casefold(), other_path.name.casefold()}
                ):
                    companions.append(target)
            if len(companions) == 1:
                add(key, companions[0], "dependency", "candidate", "A neighboring XMP sidecar matches this file's document and instance IDs; verify the association.")

        references = metadata.get("local_dependencies", [])
        if isinstance(references, (list, tuple)):
            for reference in references[:64]:
                target = _selected_reference(value, reference, paths)
                if target is not None:
                    add(key, target, "dependency", "asserted", "A local resource reference names an already selected file.")

    for members in document_ids.values():
        # Mass-produced/copied IDs are not useful version-family evidence.
        if not 2 <= len(members) <= 64:
            continue
        eligible = [key for key in members
                    if _identifier(files[key].metadata.get("xmp_instance_id"))
                    and len(instance_ids[_identifier(files[key].metadata.get("xmp_instance_id"))]) == 1]
        if len(eligible) < 2:
            continue
        anchor = eligible[0]
        for key in eligible[1:]:
            if files[key].fingerprint and files[key].fingerprint == files[anchor].fingerprint:
                continue
            add(key, anchor, "possible_version", "candidate", "A shared XMP document ID has distinct instance IDs; verify the contents.")

    return [edges[key] for key in sorted(edges)]
