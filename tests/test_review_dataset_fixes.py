"""Focused regressions from the synthetic 300-file review; no provider calls."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import chat, history
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import _prompt_item, plan_content_intelligence, suggestion_to_move
from app.core.evidence import EvidenceBundle, extract_evidence
from app.core.executor import execute_plan
from app.core.extractor import detect_text_format
from app.core.privacy import detect_sensitive, redact_sensitive
from app.models.schemas import FileInfo


def info(path):
    return FileInfo(path=str(path), filename=path.name, extension=path.suffix.lower(), size_kb=1, date_created="")


@pytest.mark.parametrize("text,kind,value", [
    ('{"aadhar": "234567890123"}', "likely_aadhaar", "234567890123"),
    ("name,aadhaar_number,city\nExample,234567890123,Example", "likely_aadhaar", "234567890123"),
    ("Passport number: P1234567", "likely_passport", "P1234567"),
    ('{"passport_number": "P1234567"}', "likely_passport", "P1234567"),
    ("Credit card number: 4111111111111112", "likely_payment_card", "4111111111111112"),
])
def test_labelled_identifiers_are_private_even_without_grouping_or_valid_checksum(text, kind, value):
    assert kind in {finding.kind for finding in detect_sensitive(text)}
    assert value not in redact_sensitive(text)


@pytest.mark.parametrize("separator,footer", [("\n", True), (" ", True), ("\n", False), (r"\n", True)])
def test_private_key_redaction_masks_complete_flattened_and_truncated_blocks(separator, footer):
    body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 4
    key = separator.join(["-----BEGIN RSA PRIVATE KEY-----", body] + (["-----END RSA PRIVATE KEY-----"] if footer else []))
    text = f"Key details: {key}"
    assert detect_sensitive(text)
    assert body not in redact_sensitive(text)
    assert "PRIVATE KEY-----" not in redact_sensitive(text)
    # Outbound redaction is defensive even if a caller failed to set findings.
    evidence = EvidenceBundle(path="key.txt", fingerprint="hash", filename="key.txt", extension=".txt", parent_directory="", content=text)
    assert body not in json.dumps(_prompt_item(0, evidence))


@pytest.mark.parametrize("text", [
    "Experimental reference P1234567 and measurement 234567890123",
    "Passport renewal checklist: bring photographs and the application form.",
    "Credit card interest calculation: e = 2.7182818284590452",
])
def test_context_words_and_scientific_numbers_do_not_flag_ordinary_notes(text):
    assert not detect_sensitive(text)


def test_filename_context_is_used_for_redaction():
    assert "P1234567" not in redact_sensitive("P1234567", ["passport.txt"])
    assert "4111111111111112" not in redact_sensitive("4111111111111112", ["card_details.txt"])


def test_sensitive_evidence_never_enters_cache_and_legacy_misses_are_purged(tmp_path):
    path = tmp_path / "cache.db"
    cache = ContentIntelligenceCache(path)
    cache.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM cache_migrations")
    values = [("secret", '{"content":"Passport number: P1234567"}'), ("normal", '{"content":"Physics notes about oscillation"}')]
    for key, payload in values:
        db.execute("INSERT INTO evidence_cache VALUES (?, 16, ?, 0)", (key, payload))
        db.execute("INSERT INTO intelligence_cache VALUES (?, 16, ?, 0)", (key, payload))
    db.commit()
    db.close()
    cache = ContentIntelligenceCache(path)
    try:
        assert cache._db.execute("SELECT fingerprint FROM evidence_cache").fetchall() == [("normal",)]
        assert cache._db.execute("SELECT cache_key FROM intelligence_cache").fetchall() == [("normal",)]
        evidence = EvidenceBundle(path="x.txt", fingerprint="new", filename="x.txt", extension=".txt", parent_directory="", content="Passport number: P1234567")
        cache.put_evidence(evidence)
        assert cache.get_evidence("new", ".txt") is None
    finally:
        cache.close()


SAMPLES = [
    ("notes.html", "import math\n\ndef area(radius):\n    return math.pi * radius ** 2\n" + "# Calculating the circular area with Python.\n" * 6, ".py", "Python"),
    ("page.csv", json.dumps({"title": "Weather readings", "stations": ["North", "South"], "readings": [21, 22, 23]}), ".json", ""),
    ("page.doc", "# Project notes\n\n- Review design requirements\n- Prepare the release checklist\n" * 4, ".md", ""),
    ("style.css", "/* Keep the function signatures stable. */\nbody { color: blue; margin: 0; }\n.card { padding: 16px; }", ".css", "CSS"),
    ("settings.yaml", "service: demo\nreplicas: 3\nports:\n  - 8080\n  - 9090\nenv: staging\n\n# Notes\n# Keep configuration stable.\n", ".yaml", "YAML"),
    ("settings.ini", "[server]\nhost=localhost\nport=8080\n" + "; Server configuration for a local development environment.\n" * 4, ".ini", "INI"),
]


def completion(messages):
    cards = json.loads(messages[-1]["content"])["files"]
    return json.dumps({"files": [{
        "id": card["id"], "category": "Dataset", "subcategory": "Items", "confidence": "high",
        "reasoning": "The content provides explicit evidence.", "evidence_quote": card["content"][:65],
        "filename_conflict": "misleading", "suggested_name": "descriptive_" + card["current_name"],
        "name_evidence": card["content"][:65],
    } for card in cards]})


@pytest.mark.parametrize("name,content,extension,language", SAMPLES)
def test_content_formats_drive_guarded_renames_and_language_metadata(tmp_path, name, content, extension, language):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    evidence = extract_evidence(info(path))
    assert evidence.detected_type == extension
    assert evidence.detected_type_confidence == "high"
    cache = ContentIntelligenceCache(tmp_path / "cache.db")
    try:
        for _ in range(2):
            result = plan_content_intelligence([info(path)], str(tmp_path), ["fake/model"], cache=cache, completion_fn=completion)
            item = result.suggestions[0]
            assert item.suggested_name.endswith(extension)
            assert item.language == language
            assert item.source_snapshot.sha256
        # Analysis only: original content and path are intact.
        assert path.read_text(encoding="utf-8") == content
    finally:
        cache.close()


def test_incomplete_or_generic_text_does_not_prove_a_code_language():
    for text in ("Python notes about def functions and import statements.", "def missing_body():", "# Weekly notes\nplain prose", "A { label: example }"):
        assert detect_text_format(text).extension == "unknown"


@pytest.mark.parametrize("separate_root", [False, True])
def test_corrected_preview_apply_history_and_undo_restore_exact_originals(tmp_path, monkeypatch, separate_root):
    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "state"))
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination" if separate_root else source
    originals = {}
    for name, content, _, _ in SAMPLES:
        path = source / name
        path.write_text(content, encoding="utf-8")
        originals[name] = path.read_bytes()
    cache = ContentIntelligenceCache(tmp_path / "cache.db")
    try:
        result = plan_content_intelligence([info(source / name) for name in originals], str(source), ["fake/model"], cache=cache, completion_fn=completion)
        moves = [suggestion_to_move(item, destination / item.category / item.subcategory / item.suggested_name) for item in result.suggestions]
        applied = execute_plan(moves, str(destination), dry_run=False, source_root=str(source))
        assert (applied.moved, applied.failed) == (len(originals), 0)
        detail = history.get_run(str(destination), 0)
        assert len(detail["moves"]) == len(originals)
        assert all(move["folder"] == "Dataset/Items" for move in detail["moves"])
        assert not any(move["restored"] for move in detail["moves"])
        assert history.undo_run(str(destination), 0)["restored"] == len(originals)
        assert all(move["restored"] for move in history.get_run(str(destination), 0)["moves"])
        assert all((source / name).read_bytes() == content for name, content in originals.items())
    finally:
        cache.close()


def test_language_rules_group_verified_formats_and_keep_documents_private_files_and_dependencies(tmp_path):
    from app.core.planner import Move
    names = ["stylesheet.txt", "page.txt", "config.txt", "server.txt", "notes.md", "secret.txt", "linked.css"]
    languages = ["CSS", "HTML", "YAML", "INI", "", "Python", "CSS"]
    moves, suggestions = [], []
    for index, (name, language) in enumerate(zip(names, languages)):
        path = tmp_path / name
        path.write_text("Untouched fixture")
        moves.append(Move(str(path), str(tmp_path / "Initial" / name), "Initial", "Initial plan"))
        suggestions.append(SimpleNamespace(language=language, sensitive=index == 5, requires_relationship_review=index == 6, selected=True, category="Initial", subcategory="", reasoning="Initial"))
    plan = SimpleNamespace(moves=moves, suggestions=suggestions, source=tmp_path, chat_decisions={})
    rule = chat.ChatRule(action="group_by_language", match={"glob": "*"}, target="Workshop", reason="Group code by language")
    result = chat.ChatResult(reply="Updated the preview.", rules=[rule], needs_clarification=None)
    refined, items, _, affected, _ = chat.refine(plan, result, {"files": []}, [])
    assert [f"{move.folder}/{move.subfolder}" for move in refined[:4]] == ["Workshop/CSS", "Workshop/HTML", "Workshop/YAML", "Workshop/INI"]
    assert refined[4].folder == "Initial"
    assert not refined[5].selected and not refined[6].selected
    assert affected == {0, 1, 2, 3}
    assert all(path.is_file() for path in tmp_path.iterdir())  # Chat only changes the preview.
    with pytest.raises(ValidationError):
        chat.ChatRule(action="group_by_language", match={"glob": "*"}, target="Too/Deep", reason="Invalid")


def test_chat_api_combines_language_grouping_and_naming_without_an_extra_classification_call(tmp_path, monkeypatch):
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app import api

    monkeypatch.setenv("ORGANIZER_HOME", str(tmp_path / "state"))
    api.plans.clear()
    source = tmp_path / "source"
    source.mkdir()
    names = []
    for name, content, _, _ in SAMPLES:
        path = source / name
        path.write_text(content, encoding="utf-8")
        names.append(path)
    cache = ContentIntelligenceCache(tmp_path / "cache.db")
    try:
        result = plan_content_intelligence([info(path) for path in names], str(source), ["fixture/model"], cache=cache, completion_fn=completion)
        moves = [suggestion_to_move(item, source / item.category / item.subcategory / item.suggested_name) for item in result.suggestions]
        api.plans["language-fixture"] = api.StoredPlan(source, moves, result.suggestions)
        rules = [
            {"action": "group_by_language", "match": {"glob": "*"}, "target": "Workshop", "reason": "Group by verified language"},
            {"action": "rename", "match": {"glob": "*"}, "target": None, "reason": "Keep original names", "naming": {"enabled": False}},
        ]
        response = json.dumps({"reply": "Review the updated language folders.", "rules": rules, "needs_clarification": None})
        with patch.object(api.provider_store, "activate_runtime", return_value=["fixture/model"]), patch("app.core.llm_http.direct_call_sync", return_value=response) as provider:
            request = TestClient(api.app).post("/api/chat", json={"plan_id": "language-fixture", "message": "Group code by language and keep original names"})
        assert request.status_code == 200, request.text
        files = {item["name"]: item for item in request.json()["preview"]["files"]}
        assert files["style.css"]["target_path"] == "Workshop/CSS"
        assert files["settings.yaml"]["target_path"] == "Workshop/YAML"
        assert files["settings.ini"]["target_path"] == "Workshop/INI"
        assert files["page.doc"]["target_path"] == "Dataset/Items"
        assert provider.call_count == 1
        assert all(path.exists() for path in names)
    finally:
        cache.close()
        api.plans.clear()
