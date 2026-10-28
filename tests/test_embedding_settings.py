"""Embedding choices/downloads must stay explicit, local and outside the bundle."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app import api
from app.core import embedding_settings as settings
from app.core.semantic_index import LocalEmbeddingClient, SemanticConfig


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('ORGANIZER_HOME',str(tmp_path/'settings'))
    monkeypatch.setenv('ORGANIZER_SEMANTIC_ENABLED','0')
    monkeypatch.setenv('ORGANIZER_EMBEDDING_MODEL','all-minilm:22m')
    monkeypatch.setenv('ORGANIZER_EMBEDDING_DEVICE','auto')
    monkeypatch.setenv('ORGANIZER_RECENT_SECONDS','0')
    monkeypatch.setattr(settings,'_download',None)


@contextmanager
def service(**changes):
    state={'requests':[], 'installed':[], 'running':[], 'progress':[], **changes}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,data):
            raw=json.dumps(data).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            state['requests'].append(('GET',self.path,None))
            if state.get('redirect'):
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/collect');self.end_headers();return
            if self.path=='/api/tags': self.respond({'models':state['installed']})
            elif self.path=='/api/ps': self.respond({'models':state['running']})
            else:self.send_error(404)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(('POST',self.path,body))
            if self.path=='/api/embed':
                self.respond({'embeddings':[[1,0] for _ in body['input']]});return
            if self.path!='/api/pull':self.send_error(404);return
            if state.get('release'):state['release'].wait(timeout=3)
            if state.get('install_on_success'):
                state['installed']=[{'name':body['model'],'digest':'a'*64}]
            raw=b''.join(json.dumps(item).encode()+b'\n' for item in state['progress'])
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
            self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
    thread.start()
    try:yield state,f'http://127.0.0.1:{server.server_port}'
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def wait_download():
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        state=settings.download_status()
        if state and state['state']!='downloading':return state
        time.sleep(.01)
    raise AssertionError('Download worker did not finish')


def test_default_and_saved_choices_apply_without_mutating_environment():
    assert settings.runtime_config().model=='all-minilm:22m'
    settings.save_preferences(True,'qwen3-embedding:0.6b','cpu')
    config=settings.runtime_config()
    assert config.enabled and config.model=='qwen3-embedding:0.6b' and config.device=='cpu'
    assert os.environ['ORGANIZER_EMBEDDING_MODEL']=='all-minilm:22m'
    assert SemanticConfig.from_env().model=='all-minilm:22m'  # CLI configuration remains independent.


def test_missing_service_is_explicit_and_never_starts_a_download():
    with patch.object(settings,'_json_request',side_effect=OSError('offline')),patch.object(settings,'Thread') as worker:
        state=settings.public_state()
    assert not state['service_available'] and state['download'] is None
    assert [row['id'] for row in state['models']]==['all-minilm:22m','qwen3-embedding:0.6b']
    assert all(not row['installed'] for row in state['models'])
    worker.assert_not_called()


def test_discovery_does_not_download_and_gpu_status_requires_running_model(monkeypatch):
    with service(installed=[{'name':'qwen3-embedding:0.6b','digest':'a'*64}],
                 running=[{'name':'qwen3-embedding:0.6b','size_vram':100000}]) as (state,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        result=settings.public_state()
    assert result['service_available']
    assert result['models'][0]['acceleration']=='not_loaded'
    assert result['models'][1]['installed'] and result['models'][1]['acceleration']=='gpu'
    assert all(method=='GET' for method,_,_ in state['requests'])


def test_cloud_or_unverifiable_rows_are_not_marked_installed(monkeypatch):
    with service(installed=[{'name':'all-minilm:22m','digest':'a'*64,'remote_host':'https://remote.invalid'},
                            {'name':'qwen3-embedding:0.6b','digest':'invalid'}]) as (_,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        assert not any(row['installed'] for row in settings.public_state()['models'])


def test_redirect_is_not_followed(monkeypatch):
    with service(redirect=True) as (state,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        assert not settings.public_state()['service_available']
    assert len(state['requests'])==1


def test_nonlocal_download_endpoint_is_rejected_before_worker(monkeypatch):
    monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL','https://remote.invalid')
    with patch.object(settings,'Thread') as worker,pytest.raises(ValueError,match='loopback'):
        settings.start_download('qwen3-embedding:0.6b')
    worker.assert_not_called()


def test_explicit_download_sends_only_supported_model_and_verifies_install(monkeypatch):
    progress=[{'status':'pulling','digest':'sha256:a','total':100,'completed':40},
              {'status':'pulling','digest':'sha256:a','total':100,'completed':100}, {'status':'success'}]
    with service(progress=progress,install_on_success=True) as (state,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        job=settings.start_download('qwen3-embedding:0.6b')
        assert job['state']=='downloading'
        done=wait_download()
    assert done['state']=='completed' and done['completed']==100 and done['total']==100
    assert next(body for method,_,body in state['requests'] if method=='POST')=={
        'model':'qwen3-embedding:0.6b','stream':True}


@pytest.mark.parametrize('progress',[[{'status':'success'}],[{'error':'private-provider-error-do-not-echo'}]])
def test_unverified_or_failed_download_is_reported_without_raw_response(monkeypatch,progress):
    with service(progress=progress) as (_,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        settings.start_download('all-minilm:22m')
        result=wait_download()
    assert result['state']=='failed'
    assert 'private-provider' not in json.dumps(result)


def test_duplicate_clicks_share_job_and_other_downloads_are_blocked(monkeypatch):
    release=threading.Event()
    with service(progress=[{'status':'success'}],install_on_success=True,release=release) as (state,url):
        monkeypatch.setenv('ORGANIZER_EMBEDDING_BASE_URL',url)
        first=settings.start_download('all-minilm:22m')
        try:
            assert settings.start_download('all-minilm:22m')['id']==first['id']
            with pytest.raises(settings.EmbeddingSettingsError):settings.start_download('qwen3-embedding:0.6b')
        finally:release.set()
        assert wait_download()['state']=='completed'
    assert sum(method=='POST' for method,_,_ in state['requests'])==1


def test_device_preference_leaves_auto_to_ollama_and_cpu_sets_request_option():
    with service(installed=[{'name':'all-minilm:22m','digest':'a'*64}]) as (state,url):
        for device in ('auto','cpu'):
            LocalEmbeddingClient(SemanticConfig(base_url=url,device=device)).embed(['safe evidence'],task='document')
    bodies=[body for method,path,body in state['requests'] if method=='POST' and path=='/api/embed']
    assert 'options' not in bodies[0] and bodies[1]['options']=={'num_gpu':0}


def test_api_rejects_other_models_before_downloading_or_saving():
    with TestClient(api.app) as client,patch.object(settings,'start_download') as start:
        assert client.post('/api/embeddings/download',json={'model':'other/model'}).status_code==422
        assert client.post('/api/embeddings/config',json={'enabled':True,'model':'other/model','device':'auto'}).status_code==422
        assert client.post('/api/embeddings/config',json={'enabled':'yes','model':'all-minilm:22m'}).status_code==422
    start.assert_not_called()


def test_failed_save_keeps_previous_preferences(tmp_path):
    settings.save_preferences(False,'all-minilm:22m','auto')
    with patch.object(settings.os,'replace',side_effect=PermissionError('locked')),pytest.raises(settings.EmbeddingSettingsError):
        settings.save_preferences(True,'qwen3-embedding:0.6b','cpu')
    assert not settings.runtime_config().enabled and settings.runtime_config().model=='all-minilm:22m'


def test_corrupt_settings_fail_visibly(tmp_path):
    settings.save_preferences(False,'all-minilm:22m','auto')
    (tmp_path/'settings/embeddings.json').write_text('broken')
    with pytest.raises(settings.EmbeddingSettingsError,match='could not be read'):
        settings.runtime_config()


def test_app_choices_remain_limited_to_two_models_but_cli_can_be_configured(monkeypatch):
    monkeypatch.setenv('ORGANIZER_EMBEDDING_MODEL','other-experimental-model')
    assert settings.runtime_config().model=='all-minilm:22m'
    assert SemanticConfig.from_env().model=='other-experimental-model'


def test_malformed_preference_fields_fail_visibly(tmp_path):
    settings.save_preferences(False,'all-minilm:22m','auto')
    path=tmp_path/'settings/embeddings.json'
    path.write_text(json.dumps({'version':1,'enabled':True,'model':[],'device':'auto'}))
    with pytest.raises(settings.EmbeddingSettingsError):settings.runtime_config()


def test_preview_uses_saved_embedding_preferences(tmp_path):
    source=tmp_path/'files';source.mkdir()
    (source/'notes.txt').write_text('Document evidence for a local folder. '*8)
    settings.save_preferences(True,'qwen3-embedding:0.6b','cpu')
    with TestClient(api.app) as client,patch.object(api.provider_store,'activate_runtime',return_value=['fixture/model']), \
        patch.object(api,'plan_content_intelligence',side_effect=RuntimeError('Stop before model calls')) as planner:
        response=client.post('/api/preview',json={'source':str(source)})
    assert response.status_code==502
    config=planner.call_args.kwargs['semantic_config']
    assert config.enabled and config.model=='qwen3-embedding:0.6b' and config.device=='cpu'
