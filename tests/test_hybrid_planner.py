"""Hybrid routing must reduce repeated work without replacing per-file validation."""
from contextlib import closing
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from pypdf import PdfWriter
import pytest

from app.core import content_intelligence as planner
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence, suggestion_to_move
from app.core.hybrid_workload import batch_hints
from app.core.relationships import FileRelationship
from app.core.semantic_index import SemanticConfig
from app.models.schemas import FileInfo


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ORGANIZER_SEMANTIC_ENABLED", "0")


def write_file(root, name, text=None):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else
                    f"Bonsai workshop field record {name}. Branch structures and pruning evidence. " * 5,
                    encoding="utf-8")
    return FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                    size_kb=path.stat().st_size / 1024, date_created="2026-01-01")


class Completion:
    def __init__(self, choose=None):
        self.payloads = []
        self.choose = choose

    def __call__(self, messages):
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        rows = []
        for card in payload["files"]:
            row = {
                "id": card["id"], "category": "Bonsai Workshop", "subcategory": "Field Records",
                "confidence": "high", "reasoning": "Supported by this file's workshop evidence.",
                "evidence_quote": card["content"][:80].strip(), "filename_conflict": "none",
                "suggested_name": "", "name_evidence": "",
            }
            if self.choose:
                row.update(self.choose(card, payload, len(self.payloads)))
            rows.append(row)
        return json.dumps({"files": rows})


class IdenticalVectors:
    model_identity = "fixture:all-identical-vectors:v1"

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, task):
        self.calls.append((task, list(texts)))
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_linked_local_assets_are_classified_but_left_for_relationship_review(tmp_path):
    root = tmp_path / "files"
    page = write_file(root, "page.html", '<html><title>Bonsai workshop</title><body>'
                      + "Bonsai workshop field records explain pruning and branch structure. " * 8
                      + '<img src="picture.txt"></body></html>')
    asset = write_file(root, "picture.txt")
    result = run(tmp_path, [page, asset], allow_rename=False)
    assert result.stats["llm_file_evaluations"] == 2
    assert result.stats["relationship_count"] >= 1
    assert all(item.requires_relationship_review and not item.selected for item in result.suggestions)
    assert all(any("file references" in warning for warning in item.warnings) for item in result.suggestions)
    assert all(Path(file.path).is_file() for file in (page, asset))


def test_unselected_or_missing_local_resource_still_requires_review(tmp_path):
    page = write_file(tmp_path / "files", "page.html", '<html><body>'
                      + "Bonsai workshop notes describe pruning and branch structure. " * 8
                      + '<img src="assets/unselected.png"></body></html>')
    result = run(tmp_path, [page], allow_rename=False)
    assert result.suggestions[0].requires_relationship_review
    assert not result.suggestions[0].selected


def test_changed_companion_invalidates_instruction_dependent_cached_decisions(tmp_path):
    root = tmp_path / "files"
    status = write_file(root, "status.txt", "Weekly project status report describes progress and outstanding work. " * 5)
    proposal = write_file(root, "proposal.txt", "Project Maple proposal defines the shared organization context. " * 5)
    instructions = ["Use the project name in the accompanying proposal as parent for these files."]
    parent = ["Maple"]
    calls = []

    def complete(messages):
        cards = json.loads(messages[-1]["content"])["files"]
        calls.append([card["current_name"] for card in cards])
        return json.dumps({"files": [{"id": card["id"], "category": parent[0], "subcategory": "Records",
                         "confidence": "high", "reasoning": "User's selected proposal supplies project context.",
                         "evidence_quote": card["content"][:60]} for card in cards]})

    with closing(ContentIntelligenceCache(tmp_path / "shared.sqlite")) as cache:
        first = plan_content_intelligence([status, proposal], str(root), ["fixture/model"],
                                          cache=cache, completion_fn=complete, instructions=instructions)
        assert {item.category for item in first.suggestions} == {"Maple"}
        Path(proposal.path).write_text("Project Cedar proposal defines the shared organization context. " * 5)
        parent[0] = "Cedar"
        current_files = [FileInfo(path=file.path, filename=file.filename, extension=file.extension,
                                 size_kb=1, date_created="") for file in (status, proposal)]
        second = plan_content_intelligence(current_files, str(root), ["fixture/model"], cache=cache,
                                           completion_fn=complete, instructions=instructions)
    assert calls == [["status.txt", "proposal.txt"], ["status.txt", "proposal.txt"]]
    assert {item.category for item in second.suggestions} == {"Cedar"}
    assert second.stats["cache_hits"] == 0


def run(root, files, completion=None, **kwargs):
    with closing(ContentIntelligenceCache(root / "classification.sqlite")) as cache:
        return plan_content_intelligence(
            files, str(root), ["fixture/model"], cache=cache,
            completion_fn=completion or Completion(), **kwargs,
        )


@pytest.mark.parametrize("count,enabled", [(1, False), (75, False), (1, True), (5, True), (74, True)])
def test_default_and_small_folder_paths_never_touch_embedding_runtime_or_cache(tmp_path, count, enabled):
    files = [write_file(tmp_path / "files", f"record-{number}.txt") for number in range(count)]
    completion, embedder = Completion(), Mock(side_effect=AssertionError("Embedding must stay optional"))
    config = SemanticConfig(enabled=enabled, min_files=75)
    with (patch("app.core.semantic_index.LocalEmbeddingClient") as local_client,
          patch("app.core.semantic_index._EmbeddingCache") as embedding_cache):
        result = run(tmp_path, files, completion, semantic_config=config, embedder=embedder,
                     embedding_cache_path=tmp_path / "unused-embedding.sqlite", allow_rename=False)
    local_client.assert_not_called()
    embedding_cache.assert_not_called()
    embedder.assert_not_called()
    assert not (tmp_path / "unused-embedding.sqlite").exists()
    assert result.stats["embedding_status"] == ("below_threshold" if enabled else "disabled")
    assert result.stats["llm_file_evaluations"] == count
    assert all(item.selected for item in result.suggestions)


def test_explicit_large_folder_opt_in_still_validates_every_file_when_vectors_match(tmp_path):
    files = []
    for number in range(75):
        purpose = "Equipment invoice" if number % 2 else "Bonsai field notes"
        files.append(write_file(tmp_path / "files", f"scan-{number}.txt",
                                f"{purpose} record number {number}. Branch structures in the bonsai workshop. " * 5))

    def choose(card, _payload, _request):
        invoice = "Equipment invoice" in card["content"]
        return {"category": "Workshop Purchases" if invoice else "Bonsai Studies",
                "subcategory": "Equipment Invoices" if invoice else "Field Notes"}

    completion, embedder = Completion(choose), IdenticalVectors()
    result = run(tmp_path, files, completion, semantic_config=SemanticConfig(enabled=True), embedder=embedder,
                 embedding_cache_path=tmp_path / "embeddings.sqlite", allow_rename=False)
    assert result.stats["embedding_status"] == "ready"
    assert result.stats["embedding_documents"] == 75
    assert result.stats["llm_file_evaluations"] == 75
    assert result.stats["duplicate_decisions_reused"] == 0
    assert sum(len(payload["files"]) for payload in completion.payloads) == 75
    for number, item in enumerate(result.suggestions):
        assert item.category == ("Workshop Purchases" if number % 2 else "Bonsai Studies")
        assert item.selected
    assert any(payload["semantic_candidates"] for payload in completion.payloads)


def test_duplicate_reuse_is_contextual_and_preserves_names_and_source_snapshots(tmp_path):
    text = "Bonsai field notes explain pruning and branch structures. " * 6
    files = [write_file(tmp_path / "files", name, text) for name in ("original.txt", "copy.txt", "third.txt")]
    completion = Completion()
    result = run(tmp_path, files, completion, allow_rename=False)
    assert result.stats["llm_file_evaluations"] == 1
    assert result.stats["duplicate_decisions_reused"] == 2
    assert [item.suggested_name for item in result.suggestions] == [file.filename for file in files]
    assert all(item.selected for item in result.suggestions)
    assert len({item.source_snapshot.identity for item in result.suggestions}) == 3
    assert len({item.source_snapshot.sha256 for item in result.suggestions}) == 1
    for file, item in zip(files, result.suggestions):
        assert item.source_snapshot == file.source_snapshot
        move = suggestion_to_move(item, tmp_path / item.category / item.subcategory / item.suggested_name)
        assert move.source_snapshot == file.source_snapshot
        assert Path(move.src) == Path(file.path)


@pytest.mark.parametrize("mode", ["rename", "instructions", "different_parent", "same_leaf_different_parent", "sensitive"])
def test_duplicates_are_evaluated_individually_when_context_requires_it(tmp_path, mode):
    text = "Bonsai field notes explain pruning and branch structures. " * 6
    folder_a, folder_b = tmp_path / "files", tmp_path / "files"
    if mode == "different_parent":
        folder_a, folder_b = tmp_path / "alpha", tmp_path / "beta"
    elif mode == "same_leaf_different_parent":
        folder_a, folder_b = tmp_path / "alpha" / "notes", tmp_path / "beta" / "notes"
    elif mode == "sensitive":
        text = "API_KEY='sk-fixture-abcdefghijklmnopqrstuvwxyz0123456789'\n" + text
    files = [write_file(folder_a, "first.txt", text), write_file(folder_b, "second.txt", text)]
    result = run(tmp_path, files, allow_rename=mode == "rename",
                 instructions=["Keep copies separate by their source filenames."] if mode == "instructions" else None,
                 allow_sensitive_cloud=mode == "sensitive")
    assert result.stats["llm_file_evaluations"] == 2
    assert result.stats["duplicate_decisions_reused"] == 0
    if mode == "sensitive":
        assert all(item.sensitive and not item.selected for item in result.suggestions)


def test_explicit_file_instructions_survive_duplicate_folder_reconciliation(tmp_path):
    text = "Bonsai field notes explain pruning and branch structures. " * 6
    files = [write_file(tmp_path / "files", name, text) for name in ("active.txt", "archived.txt")]

    def choose(card, _payload, _request):
        return {"category": "Active Exhibition" if card["current_name"] == "active.txt" else "Exhibition Archive",
                "subcategory": "Workshop Notes"}

    result = run(tmp_path, files, Completion(choose), allow_rename=False,
                 instructions=["Put active.txt under Active Exhibition and archived.txt under Exhibition Archive."])
    assert [item.category for item in result.suggestions] == ["Active Exhibition", "Exhibition Archive"]
    assert result.stats["duplicate_decisions_reused"] == 0


def test_weak_representative_falls_back_to_individual_copy_evaluations(tmp_path):
    text = "Bonsai field notes explain pruning and branch structures. " * 6
    files = [write_file(tmp_path / "files", name, text) for name in ("first.txt", "second.txt", "third.txt")]

    def choose(_card, _payload, request):
        return {"confidence": "insufficient", "category": "Misc", "subcategory": "General"} if request == 1 else {}

    completion = Completion(choose)
    result = run(tmp_path, files, completion, allow_rename=False)
    assert result.stats["llm_file_evaluations"] == 3
    assert result.stats["duplicate_decisions_reused"] == 0
    assert len(completion.payloads) == 2
    assert not result.suggestions[0].selected
    assert all(item.selected for item in result.suggestions[1:])


def test_user_instruction_changes_invalidate_cached_decisions(tmp_path):
    files = [write_file(tmp_path / "files", "record.txt")]

    def choose(_card, payload, _request):
        return {"category": "Amber Exhibition" if "Amber Exhibition" in payload["organization_instructions"][0]
                else "Living Collections", "subcategory": "Workshop Records"}

    completion = Completion(choose)
    with closing(ContentIntelligenceCache(tmp_path / "cache.sqlite")) as cache:
        def plan(instructions):
            return plan_content_intelligence(files, str(tmp_path), ["fixture/model"], cache=cache,
                                             completion_fn=completion, instructions=instructions, allow_rename=False)
        first = plan(["Use Amber Exhibition as the parent category."])
        unchanged = plan(["Use Amber Exhibition as the parent category."])
        changed = plan(["Use Living Collections as the parent category."])
    assert first.stats["cache_hits"] == 0
    assert unchanged.stats["cache_hits"] == 1
    assert changed.stats["cache_hits"] == 0
    assert len(completion.payloads) == 2
    assert changed.suggestions[0].category == "Living Collections"


@pytest.mark.parametrize("kind", ["weak_text", "title_only", "dimensions_only", "creator_only"])
def test_insufficient_semantic_evidence_is_unselected_in_the_complete_planner(tmp_path, kind):
    if kind == "weak_text":
        file = write_file(tmp_path / "files", "short.txt", "Pruning")
    elif kind == "dimensions_only":
        path = tmp_path / "photo.png"
        Image.new("RGB", (1600, 1200), "white").save(path)
        file = FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                        size_kb=1, date_created="2026-01-01")
    else:
        path = tmp_path / "scan.pdf"
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        writer.add_metadata({"/Title": "Bonsai Workshop Field Records"} if kind == "title_only"
                            else {"/Creator": "A scanner", "/Title": "Untitled"})
        with path.open("wb") as output:
            writer.write(output)
        file = FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                        size_kb=1, date_created="2026-01-01")
    completion = Completion()
    with patch("app.core.evidence.shutil.which", return_value=None):
        result = run(tmp_path, [file], completion)
    assert not result.suggestions[0].selected
    assert not result.suggestions[0].sufficient_for_change
    assert result.stats["needs_review"] == 1
    if kind in {"dimensions_only", "creator_only"}:
        assert result.stats["llm_file_evaluations"] == 0


def test_semantic_and_relationship_hints_use_only_current_batch_ids(tmp_path):
    files = [write_file(tmp_path / "files", f"item-{number}.txt") for number in range(7)]
    completion, embedder = Completion(), IdenticalVectors()
    edges = [FileRelationship(2, 3, "possible_version", "candidate", "fixture metadata")]
    with patch.object(planner, "relationship_groups", return_value=edges):
        result = run(tmp_path, files, completion, batch_size=2,
                     semantic_config=SemanticConfig(enabled=True, min_files=0), embedder=embedder,
                     embedding_cache_path=tmp_path / "semantic.sqlite", allow_rename=False)
    for payload in completion.payloads:
        known = {card["id"] for card in payload["files"]}
        for hint in payload["semantic_candidates"] + payload["relationships"]:
            assert hint["a"] in known and hint["b"] in known
            assert hint["a"] != hint["b"]
    assert any(payload["relationships"] for payload in completion.payloads)
    assert result.stats["llm_file_evaluations"] == len(files)
    assert {item.target_path for item in result.suggestions} == {"Bonsai Workshop/Field Records"}


def test_unavailable_embedding_backend_is_visible_and_keeps_per_file_planning(tmp_path):
    files = [write_file(tmp_path / "files", f"file-{number}.txt") for number in range(3)]
    embedder = Mock(side_effect=RuntimeError("Fixture local backend is offline"))
    result = run(tmp_path, files, semantic_config=SemanticConfig(enabled=True, min_files=0), embedder=embedder,
                 embedding_cache_path=tmp_path / "semantic.sqlite", allow_rename=False)
    assert result.stats["embedding_status"] == "unavailable"
    assert result.stats["analysis_warnings"]
    assert result.stats["llm_file_evaluations"] == 3
    assert all(item.selected for item in result.suggestions)


@pytest.mark.parametrize("shape,count", [("identical", 500), ("orthogonal", 200)])
def test_large_direct_batches_bound_candidate_comparisons_and_hint_construction(shape, count):
    batch = [(SimpleNamespace(path=f"file-{number}.txt"), None, None) for number in range(count)]
    ids = {item[0].path: number for number, item in enumerate(batch)}
    vectors = {number: (1.0, 0.0) if shape == "identical" else
               tuple(float(number == dimension) for dimension in range(count))
               for number in range(count)}
    with patch("app.core.hybrid_workload.math.fsum", wraps=math.fsum) as score:
        result = batch_hints(batch, [], SimpleNamespace(vectors=vectors), ids, [])
    assert score.call_count <= 256
    assert len(result["semantic_candidates"]) <= 64
    if shape == "identical":
        assert score.call_count == 64
        assert len(result["semantic_candidates"]) == 64
    else:
        assert score.call_count == 256
        assert result["semantic_candidates"] == []
    assert all(hint["a"] in ids.values() and hint["b"] in ids.values()
               for hint in result["semantic_candidates"])


def test_relationship_hint_cap_stops_constructing_discarded_dictionaries():
    yielded = []

    def edges():
        for number in range(10000):
            yielded.append(number)
            yield FileRelationship(0, 1, "possible_version", "candidate", "fixture metadata")

    batch = [(SimpleNamespace(path="first.txt"), None, None),
             (SimpleNamespace(path="second.txt"), None, None)]
    result = batch_hints(batch, [], SimpleNamespace(vectors={}), {"first.txt": 0, "second.txt": 1}, edges())
    assert len(result["relationships"]) == 64
    assert len(yielded) == 64
