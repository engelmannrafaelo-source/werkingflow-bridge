from unittest.mock import AsyncMock, MagicMock
import json
import httpx
import pytest
from fastapi import HTTPException
from src.providers import credit_pool, credit_pool_routes
from src.providers.funding_policy import promotional_only, require_pool
from src.research_cloud.executor import run_research_cloud, ResearchCloudExecutorError
from src.research_cloud.models import ResearchCloudConfig
from src.research_cloud.library import LibraryConfig
from src.research_cloud.perplexity import PerplexityConfig
from src.research_cloud.luecken import messages_api_pruefer, Nachkontrolle


def test_scope_and_missing_coordinator(monkeypatch):
    from starlette.requests import Request
    assert promotional_only(Request({'type':'http','headers':[(b'x-app-id',b'werking-energy')]}))
    assert not promotional_only(Request({'type':'http','headers':[(b'x-app-id',b'werking-report')]}))
    assert not promotional_only(None)
    monkeypatch.delenv('ANTHROPIC_CREDIT_POOL_URL',raising=False)
    with pytest.raises(HTTPException): require_pool()

@pytest.mark.asyncio
async def test_bounded_tools_counted_reserved_and_settled(monkeypatch):
    events=[]
    async def coord(method,path,data=None):
        events.append((path,data))
        return {'accounts':[{'id':'a','org_id':'o','grant_id':'g','api_key':'free-key'}]}
    async def handler(req):
        body=json.loads(req.content)
        assert req.headers['x-api-key']=='free-key'
        if req.url.path.endswith('count_tokens'):
            assert body['tools'][0]['max_uses']==3
            return httpx.Response(200,json={'input_tokens':100})
        assert events[-1][0]=='/reserve'
        return httpx.Response(200,json={'usage':{'input_tokens':300,'output_tokens':10,'cache_read_input_tokens':0,'cache_creation_input_tokens':0,'server_tool_use':{'web_search_requests':2}},'content':[]})
    original=httpx.AsyncClient
    monkeypatch.setattr(credit_pool,'coordinator',coord)
    monkeypatch.setattr(credit_pool.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    _,meta=await credit_pool.post_messages({'model':'claude-sonnet-5-5','max_tokens':100,'messages':[], 'tools':[{'type':'web_search_20250305','name':'web_search','max_uses':3}]},10)
    reserve=next(d for p,d in events if p=='/reserve')
    assert reserve['input_tokens']==8_000_000 and reserve['max_tokens']==400
    assert meta['funding_policy']=='promotional_only'
    usage=events[-1][1]['usage']
    assert credit_pool_routes.usage_cost('claude-sonnet-5-5',usage)>0.02

@pytest.mark.asyncio
async def test_unpriced_tools_never_contact_provider(monkeypatch):
    coord=AsyncMock();monkeypatch.setattr(credit_pool,'coordinator',coord)
    with pytest.raises(HTTPException):
        await credit_pool.post_messages({'tools':[{'type':'code_execution_20250825'}]},10)
    coord.assert_not_awaited()

@pytest.mark.asyncio
async def test_research_uses_pool_each_round_not_legacy_or_perplexity(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL','http://pool')
    monkeypatch.delenv('RESEARCH_CLOUD_API_KEY',raising=False)
    calls=[]
    async def post(body,timeout):
        calls.append(body)
        assert all(t['name']!='perplexity_search' for t in body['tools'])
        assert all('20260209' not in t.get('type','') for t in body['tools'])
        return httpx.Response(200,json={'model':'claude-sonnet-5-5','stop_reason':'pause_turn' if len(calls)==1 else 'end_turn','content':[{'type':'text','text':'complete'}],'usage':{'input_tokens':1,'output_tokens':1}}),{}
    monkeypatch.setattr(credit_pool,'post_messages',post)
    legacy=MagicMock();legacy.post=AsyncMock(side_effect=AssertionError('paid transport reached'))
    result=await run_research_cloud('query','system',config=ResearchCloudConfig(promotional_only=True),library_config=LibraryConfig(enabled=False),perplexity_config=PerplexityConfig(enabled=True,api_key='paid'),client=legacy)
    assert result.status=='success' and len(calls)==2
    legacy.post.assert_not_awaited()

@pytest.mark.asyncio
async def test_empty_pool_stops_research_and_checker(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL','http://pool')
    post=AsyncMock(side_effect=HTTPException(429,'No eligible promotional budget'))
    monkeypatch.setattr(credit_pool,'post_messages',post)
    with pytest.raises(ResearchCloudExecutorError):
        await run_research_cloud('query','system',config=ResearchCloudConfig(promotional_only=True),library_config=LibraryConfig(enabled=False),perplexity_config=PerplexityConfig(enabled=False))
    checker=Nachkontrolle(pruefer=messages_api_pruefer('paid-must-not-be-used',promotional_only=True))
    with pytest.raises(HTTPException):
        await checker.entscheide('report',[],perplexity_rest=3)
