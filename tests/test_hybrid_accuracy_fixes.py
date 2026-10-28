"""Dataset planning, naming and dependency regressions from the accuracy review."""
from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import patch

from fastapi import HTTPException
import pytest

from app import api
from app.core import safety
from app.core import content_intelligence as intelligence
from app.core.semantic_index import SemanticConfig, SemanticIndex
from app.core.evidence import EvidenceBundle
from app.core.content_cache import ContentIntelligenceCache
from app.core.content_intelligence import plan_content_intelligence
from app.core.folder_planning import parse_folder_plan
from app.core.naming import preserve_name_tokens
from app.core.planner import Move
from app.models.schemas import FileInfo


def info(path):
    return FileInfo(path=str(path), filename=path.name, extension=path.suffix,
                    size_kb=1, date_created="2026-01-01")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("ORGANIZER_SEMANTIC_ENABLED", "0")
    monkeypatch.setenv("ORGANIZER_CONTENT_CONCURRENCY", "2")


def test_ordinary_rename_preserves_date_and_version_without_another_model_call(tmp_path):
    source = tmp_path / "Physics_Lecture_v2_2026-10-14.txt"
    source.write_text("Thermal equilibrium lecture notes explain heat flow and temperature. " * 8)
    calls = []
    def complete(messages):
        calls.append(messages)
        return json.dumps({"files": [{"id": 0, "category": "Physics", "subcategory": "Lecture Notes",
             "confidence": "high", "evidence_quote": "Thermal equilibrium lecture notes",
             "suggested_name": "Thermal_Equilibrium.txt", "name_evidence": "Thermal equilibrium"}]})
    with closing(ContentIntelligenceCache(tmp_path / 'cache.sqlite')) as cache:
        result = plan_content_intelligence([info(source)], str(tmp_path), ['fixture/model'],
                                          cache=cache, completion_fn=complete)
    assert result.suggestions[0].suggested_name == "Thermal_Equilibrium_v2_2026-10-14.txt"
    assert len(calls) == 1 and result.suggestions[0].selected


def test_length_budget_keeps_complete_identifiers_and_compound_extension():
    original = "Long_Archive_v2.1_2026-10-14.tar.gz"
    name = preserve_name_tokens("Descriptive_" * 8 + ".tar.gz", original, 64)
    assert name.endswith('_v2.1_2026-10-14.tar.gz') and len(name) <= 64
    assert preserve_name_tokens("Name.txt", original, 10) is None


def test_transient_provider_connection_is_retried_once():
    reply=json.dumps({'files':[{'id':0,'category':'Ceramics','subcategory':'Observations','confidence':'low'}]})
    telemetry={}
    with patch.object(intelligence.llm_http,'direct_call_sync',side_effect=[
        intelligence.llm_http.LLMHTTPError('cannot connect to fixture (reset)',404),reply]) as call, \
        patch.object(intelligence.time,'sleep'):
        assert intelligence._model_call(['fixture/model'],[],{0},telemetry)==reply
    assert call.call_count==2 and telemetry['llm_provider_attempts']==2


def test_minilm_compacts_long_documents_and_does_not_truncate_long_chat_queries(tmp_path):
    class Embedder:
        model_identity='fixture:minilm'
        def embed(self,texts,*,task):
            assert all(len(text)<=480 for text in texts)
            return [[1,0] for _ in texts]
    card=EvidenceBundle(path='/chosen/notes.txt',fingerprint='a'*64,filename='notes.txt',extension='.txt',
                        parent_directory='chosen',content='Workshop observations. '*1000,extraction_status='sufficient')
    index=SemanticIndex.build({0:card},config=SemanticConfig(enabled=True,min_files=0),
                              embedder=Embedder(),cache_path=tmp_path/'vectors.sqlite')
    assert index.stats['embedding_status']=='ready'
    assert index.search('unbounded instruction '*100)==[] and index.warnings


def test_shared_dataset_folder_plan_precedes_bounded_parallel_decisions(tmp_path):
    files = []
    for number in range(16):
        path = tmp_path / f"item-{number}.txt"
        path.write_text(("Kiln firing schedule" if number % 2 == 0 else "Glaze trial observations")
                        + f" batch {number}. " + "Ceramic test pieces require documented conditions and outcomes. " * 5)
        files.append(info(path))
    catalog = [{"category": "Ceramics", "subcategory": "Firing Schedules",
                "purpose": "Kiln firing schedules, distinct from glaze trial observations.", "evidence_ids": [0]},
               {"category": "Ceramics", "subcategory": "Glaze Trials",
                "purpose": "Glaze trial observations, distinct from kiln firing schedules.", "evidence_ids": [1]}]
    barrier = threading.Barrier(2, timeout=5)
    payloads = []
    def complete(messages):
        payload = json.loads(messages[-1]['content'])
        payloads.append(payload)
        barrier.wait()
        return json.dumps({'files': [{'id': card['id'], 'category': 'Ceramics',
            'subcategory': 'Firing Schedules' if 'Kiln firing' in card['content'] else 'Glaze Trials',
            'confidence': 'high', 'evidence_quote': card['content'][:80]}
            for card in payload['files']]})
    with closing(ContentIntelligenceCache(tmp_path / 'cache.sqlite')) as cache:
        result = plan_content_intelligence(files, str(tmp_path), ['fixture/model'],
            known_folders=['Ceramics/Invoices'], cache=cache, completion_fn=complete,
            folder_completion_fn=lambda messages: json.dumps({'folders': catalog}))
    assert result.stats['classification_workers'] == 2
    assert result.stats['folder_plan_status'] == 'ready'
    assert result.stats['llm_batches'] == 2 and result.stats['llm_provider_attempts'] == 3
    assert all(len(payload['folder_plan']) == 2 for payload in payloads)
    for number, suggestion in enumerate(result.suggestions):
        assert suggestion.subcategory == ('Firing Schedules' if number % 2 == 0 else 'Glaze Trials')
        assert suggestion.selected and suggestion.source_snapshot.sha256


@pytest.mark.parametrize('bad', [
    {'category':'Other','subcategory':'../Escape','purpose':'Unsafe paths','evidence_ids':[0]},
    {'category':'Other','subcategory':'Notes','purpose':'Unselected example','evidence_ids':[99]},
    {'category':'Other','subcategory':'Notes','purpose':'No example','evidence_ids':[]},
])
def test_folder_plan_rejects_unsafe_or_unselected_evidence(bad):
    with pytest.raises(ValueError):
        parse_folder_plan(json.dumps({'folders':[bad]}), 16)


def test_invalid_folder_plan_falls_back_visibly_without_losing_files(tmp_path):
    files=[]
    for number in range(16):
        path=tmp_path/f'record-{number}.txt'
        path.write_text(f'Ceramics workshop record {number}. ' + 'Glaze trials document ceramic surface observations. '*5)
        files.append(info(path))
    def complete(messages):
        cards=json.loads(messages[-1]['content'])['files']
        return json.dumps({'files':[{'id':c['id'],'category':'Ceramics','subcategory':'Observations',
                          'confidence':'high','evidence_quote':c['content'][:80]} for c in cards]})
    with closing(ContentIntelligenceCache(tmp_path/'cache.sqlite')) as cache:
        result=plan_content_intelligence(files,str(tmp_path),['fixture/model'],cache=cache,
                                        completion_fn=complete,folder_completion_fn=lambda _: '{}')
    assert len(result.suggestions)==16 and all(s.selected for s in result.suggestions)
    assert result.stats['folder_plan_status']=='unavailable' and result.stats['analysis_warnings']
    assert result.stats['classification_workers']==1


def dependency_plan(tmp_path, page_destination, style_destination, *, select_page=True, select_style=True):
    page,style=tmp_path/'guide.html',tmp_path/'guide_style.css'
    suggestion=SimpleNamespace(path=str(page),local_dependencies=['guide_style.css'])
    plan=api.StoredPlan(tmp_path,[],suggestions=[suggestion])
    moves=[Move(str(page),str(page_destination),'Pages','',selected=select_page),
           Move(str(style),str(style_destination),'Pages','',selected=select_style)]
    return plan,moves


def test_explicit_acceptance_cannot_break_stylesheet_reference(tmp_path):
    plan,moves=dependency_plan(tmp_path,tmp_path/'Pages/guide.html',tmp_path/'Pages/renamed.css')
    with pytest.raises(HTTPException,match='local file reference') as error:
        api._validate_dependency_edits(plan,moves)
    assert error.value.status_code==409


def test_dependency_bundle_can_move_when_references_are_preserved(tmp_path):
    plan,moves=dependency_plan(tmp_path,tmp_path/'Pages/renamed_guide.html',tmp_path/'Pages/guide_style.css')
    api._validate_dependency_edits(plan,moves)


def test_moving_only_the_asset_cannot_break_an_unselected_page(tmp_path):
    plan,moves=dependency_plan(tmp_path,tmp_path/'Pages/guide.html',tmp_path/'Pages/guide_style.css',select_page=False)
    with pytest.raises(HTTPException): api._validate_dependency_edits(plan,moves)


def test_fresh_path_validation_still_blocks_a_new_project_between_passes(tmp_path):
    source=tmp_path/'notes.txt'
    source.write_text('content')
    target=tmp_path/'Documents/notes.txt'
    safety.validate_move_paths(source,target,tmp_path)
    (tmp_path/'package.json').write_text('{}')
    with pytest.raises(safety.UnsafePathError):
        safety.validate_move_paths(source,target,tmp_path)


def test_parent_symlink_within_approved_tree_is_still_rejected(tmp_path):
    actual=tmp_path/'actual';actual.mkdir()
    (actual/'notes.txt').write_text('content')
    link=tmp_path/'link'
    try: link.symlink_to(actual,target_is_directory=True)
    except OSError: pytest.skip('Symlink creation is unavailable')
    with pytest.raises(safety.UnsafePathError):
        safety.validate_move_paths(link/'notes.txt',tmp_path/'Other/notes.txt',tmp_path)
