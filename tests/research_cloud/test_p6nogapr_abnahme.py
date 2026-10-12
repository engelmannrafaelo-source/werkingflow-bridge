import sys, subprocess, types
from unittest.mock import AsyncMock, MagicMock, patch
import pytest, httpx
from tests.research_cloud import test_execute_research_cloud_impl as cloud
from tests.research_cloud import test_luecken_pool as pool
from tests.jobs.test_job_idempotenz_brref2 import mem, _post, KEY
from src.models import ResearchRequest
from src.jobs import store, executors
import src.main as main
from starlette.requests import Request

@pytest.mark.parametrize('value', ['missing', None, True, False])
@pytest.mark.parametrize('enabled', [True, False])
async def test_cloud_switch_and_visible_status(monkeypatch, value, enabled):
    monkeypatch.setenv('RESEARCH_PERPLEXITY_ENABLED', 'true')
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'pplx-test')
    monkeypatch.setenv('BRIDGE_ANONYMIZE_ENABLED', 'true')
    monkeypatch.setenv('RESEARCH_LUECKEN_NACHKONTROLLE', str(enabled).lower())
    req = ResearchRequest(query='Neutral review question', **({} if value == 'missing' else {'luecken_nachkontrolle': value}))
    run = AsyncMock(return_value=cloud._cloud_result())
    with patch('src.research_cloud.anonymize_gate.anonymize_query_for_cloud', AsyncMock(return_value='neutral')), patch('src.research_cloud.executor.run_research_cloud', run), patch('src.activity.ai_call_writer.persist_ai_call_activity', AsyncMock()), patch('src.scholarly.scholarly_enabled', return_value=False):
        result = await main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})
    assert result.status == 'success'
    assert (run.await_args.kwargs['nachkontrolle'] is not None) == (enabled and value is not False)
    if value is False:
        assert result.nachkontrolle and result.nachkontrolle['status'] == 'uebersprungen'

@pytest.mark.parametrize('enabled', [True, False])
async def test_pool_skip_visible_when_deployment_off_too(monkeypatch, tmp_path, enabled):
    monkeypatch.setenv('RESEARCH_PERPLEXITY_ENABLED', 'true')
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'pplx-test')
    monkeypatch.setenv('BRIDGE_ANONYMIZE_ENABLED', 'true')
    monkeypatch.setenv('RESEARCH_LUECKEN_NACHKONTROLLE', str(enabled).lower())
    fake = pool.FakeCli(tmp_path, mit_datei=True)
    req = pool._make_req()
    req.luecken_nachkontrolle = False
    with patch('src.activity.ai_call_writer.persist_ai_call_activity', AsyncMock()):
        result = await pool._run(fake, req)
    assert result.status == 'success'
    assert not fake.calls['pruefer'] and not fake.calls['rueckrunde']
    assert result.nachkontrolle and result.nachkontrolle['status'] == 'uebersprungen'

@pytest.mark.parametrize('value', ['missing', None, True, False])
async def test_peer_and_job_forwarding(value):
    body = {'query': 'neutral', **({} if value == 'missing' else {'luecken_nachkontrolle': value})}
    request = Request({'type':'http','method':'POST','path':'/v1/research','headers':[], 'server':('bridge.test',80), 'scheme':'http', 'query_string':b''})
    request.state.cached_body_dict = body
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={'status':'success'})
    with patch.object(main, '_pick_alternative_worker', AsyncMock(return_value='peer.test')), patch('httpx.AsyncClient') as cls:
        cls.return_value.__aenter__.return_value = client
        result = await main._cross_worker_retry(request, 'self.test')
    assert result.status_code == 200
    assert client.post.await_args.kwargs['json'] == body
    post = AsyncMock(return_value={'status':'success','nachkontrolle':{'status':'uebersprungen'}})
    with patch.object(executors, '_self_post_json', post):
        result = await executors.research_executor(body, {}, AsyncMock())
    assert post.await_args.args[1] == {**body, 'async_mode':False}
    assert result['nachkontrolle']['status'] == 'uebersprungen'

@pytest.mark.parametrize('initial', [{}, {'luecken_nachkontrolle':False}])
async def test_replay_and_changed_flag_rejected(mem, initial):
    body = {'kind':'idem-probe', 'payload':{'query':'neutral', **initial}}
    first = await _post(body, key=KEY)
    replay = await _post(body, key=KEY)
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()['job_id'] == replay.json()['job_id']
    changed = {'kind':'idem-probe', 'payload':{'query':'neutral', 'luecken_nachkontrolle': not initial.get('luecken_nachkontrolle', True)}}
    conflict = await _post(changed, key=KEY)
    assert conflict.status_code == 422
    assert conflict.json()['error']['code'] == 'idempotency_key_reused'

def test_old_model_ignores_field_and_digest_contains_it():
    source = subprocess.check_output(['git','show','9af3f0f:src/models.py'], text=True)
    mod = types.ModuleType('p6nogapr_old_models'); sys.modules[mod.__name__] = mod
    exec(compile(source, 'old_models.py', 'exec'), mod.__dict__)
    body = {'query':'neutral', 'luecken_nachkontrolle':False}
    assert 'luecken_nachkontrolle' not in mod.ResearchRequest(**body).model_dump()
    assert ResearchRequest(**body).luecken_nachkontrolle is False
    assert store.payload_digest(body) != store.payload_digest({'query':'neutral'})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    def endpoint(body: mod.ResearchRequest):
        return body.model_dump()
    app.post('/v1/research')(endpoint)
    result = TestClient(app).post('/v1/research', json=body)
    assert result.status_code == 200 and 'luecken_nachkontrolle' not in result.json()

@pytest.mark.parametrize('value', ['missing', None, True])
@pytest.mark.parametrize('enabled', [True, False])
async def test_pool_default_matches_deployment(monkeypatch, tmp_path, value, enabled):
    monkeypatch.setenv('RESEARCH_PERPLEXITY_ENABLED', 'true')
    monkeypatch.setenv('PERPLEXITY_API_KEY', 'pplx-test')
    monkeypatch.setenv('BRIDGE_ANONYMIZE_ENABLED', 'true')
    monkeypatch.setenv('RESEARCH_LUECKEN_NACHKONTROLLE', str(enabled).lower())
    fake = pool.FakeCli(tmp_path, mit_datei=True)
    req = pool._make_req()
    if value != 'missing': req.luecken_nachkontrolle = value
    with patch('src.activity.ai_call_writer.persist_ai_call_activity', AsyncMock()):
        result = await pool._run(fake, req)
    assert result.status == 'success'
    assert len(fake.calls['pruefer']) == (2 if enabled else 0)
    assert len(fake.calls['rueckrunde']) == (1 if enabled else 0)
    assert result.content == (pool.NEU if enabled else pool.ALT)
