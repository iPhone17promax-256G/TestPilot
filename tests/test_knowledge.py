"""Manual project-only RAG, actual Qdrant storage, lease/permission boundaries and safe provider failures."""
import json
import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from testpilot.api import app
from testpilot import knowledge as rag,executors,sources
from testpilot.config import settings
from testpilot.storage import session,Record,uid,update_record
from testpilot.workspace import project_scope,user_scope
from testpilot.knowledge_vectors import PLAN_URL,MODEL,Vectors,digest,split_text,embed,point_id,lexical_rank,fuse


@pytest.fixture
def library(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,"data",tmp_path)
    client=TestClient(app, base_url="https://testserver",headers={"Authorization":"Bearer "+settings.access_token})
    project=client.post('/api/projects',json={'name':'RAG project '+uid('AUDIT')}).json()['id']
    # 全局 API Token 创建的是公共项目；这个夹具显式指定负责人，避免绕过生产配置权限。
    with session() as db:
        row=db.get(Record,project);row.payload=dict(row.payload,ownerId='service')
    with user_scope('service'),project_scope(project):
        executor=executors.register('','RAG audit',{})
        monkeypatch.setattr(executors,'selected_executor',lambda identity='':executor['id'])
        yield client,project,executor
        for entry in rag.manifest().get('documents',{}).values():
            Vectors().delete(entry['collection'],entry['pointIds'])
        with session() as db:
            for row in db.scalars(select(Record).where((Record.project_id==project)|(Record.id==project))):db.delete(row)


def configure():return rag.save_config(rag.KnowledgeConfig(chunkSize=800,overlap=80))
def text_source(name='安全需求',text='连续认证失败五次后冻结账户十五分钟。'):
    return sources.save_source(name,text)['id']
def begin(executor):
    action=rag.start_index();leased=executors.claim(executor['id'],'local')
    assert leased['recordId']==action['id'];return action,leased['claim']
def publish(token,source,cfg):
    value=sources.source_text(sources.read_record(source,'source'))
    pieces=split_text(value,cfg['chunkSize'],cfg['overlap'])
    rag.commit_document(token,source,digest(value),[[1.0]+[0.0]*15 for _ in pieces],3)


def test_no_embedding_or_queue_before_manual_button(library,monkeypatch):
    client,project,executor=library;text_source();configure()
    monkeypatch.setattr(rag,'query_embedding',lambda *a:pytest.fail('automatic embedding'))
    assert rag.status()['pending']==1 and not rag.status()['built']
    assert rag.latest_action() is None
    assert rag.search('冻结')['mode']=='keyword'
    assert rag.latest_action() is None


def test_index_only_project_sources_and_not_chat_or_other_records(library):
    client,project,executor=library;source=text_source();configure()
    with session() as db:
        for kind in ['attachment','task','case','skill','run']:
            db.add(Record(id=uid('OTHER'),kind=kind,project_id=project,payload=dict(name='private',content='NOT PROJECT SOURCE')))
        db.add(Record(id=uid('SRC'),kind='source',project_id='different-project',payload=dict(name='outside',content='OUTSIDE PROJECT')))
    archived=text_source('归档');sources.archive_source(archived,True)
    opaque=client.post('/api/sources/upload',params={'filename':'opaque.zip'},headers={'X-TestPilot-Project':project},content=b'opaque').json()
    action,token=begin(executor);plan=rag.index_plan(token)
    assert [s['id'] for s in plan['documents']]==[source]
    assert rag.status()['unreadable']==1
    assert action['total']==1
    executors.finish(token)


def test_duplicate_button_uses_one_job_and_cancel_invalidates_writes(library):
    client,project,executor=library;text_source();configure()
    first=rag.start_index();second=rag.start_index()
    assert first['id']==second['id'] and first['dispatchId']==second['dispatchId']
    lease=executors.claim(executor['id'],'local')
    rag.cancel_index()
    with pytest.raises(PermissionError):rag.index_plan(lease['claim'])


def test_real_qdrant_publish_query_reuse_and_archive(library,monkeypatch):
    client,project,executor=library;source=text_source();cfg=configure()
    action,token=begin(executor);publish(token,source,cfg);rag.finish_index(token);executors.finish(token)
    monkeypatch.setattr(rag,'query_embedding',lambda *a:[1.0]+[0.0]*15)
    hit=rag.search('输错密码锁号',mode='semantic')['items'][0]
    assert hit['id']==source and '冻结' in hit['summary'] and hit['version']
    assert sources.read_source(source,offset=hit['start'])['content'].startswith(hit['summary'])
    next_action,next_token=begin(executor)
    assert rag.index_plan(next_token)['documents'][0]['reuse']
    rag.commit_document(next_token,source,hit['version'],[],0,True)
    rag.finish_index(next_token);executors.finish(next_token)
    assert rag.status()['job']['reused']==1 and rag.status()['job']['tokens']==0
    sources.archive_source(source,True)
    assert rag.search('冻结')['items']==[] and rag.status()['indexed']==0
    entry=rag.manifest()['documents'][source]
    Vectors().delete(entry['collection'],entry['pointIds'])


def test_new_document_is_pending_without_embedding(library,monkeypatch):
    client,project,executor=library;first=text_source();cfg=configure()
    action,token=begin(executor);publish(token,first,cfg);rag.finish_index(token);executors.finish(token)
    second=text_source('接口说明','GET /api/ping 返回 HTTP 200。')
    monkeypatch.setattr(rag,'query_embedding',lambda *a:pytest.fail('upload triggered embedding'))
    state=rag.status();assert state['indexed']==1 and state['pending']==1
    assert rag.latest_action()['id']==action['id']
    entry=rag.manifest()['documents'][first];Vectors().delete(entry['collection'],entry['pointIds'])


def test_qdrant_fault_falls_back_with_reason(library,monkeypatch):
    client,project,executor=library;source=text_source();cfg=configure()
    action,token=begin(executor);publish(token,source,cfg);rag.finish_index(token);executors.finish(token)
    monkeypatch.setattr(rag,'query_embedding',lambda *a:(_ for _ in ()).throw(ValueError('额度不足')))
    result=rag.search('冻结')
    assert result['mode']=='keyword_fallback' and result['warning']=='额度不足'
    assert result['items'][0]['id']==source
    entry=rag.manifest()['documents'][source];Vectors().delete(entry['collection'],entry['pointIds'])


def test_query_enforces_project_and_published_point_ids(monkeypatch):
    seen=[]
    monkeypatch.setattr(Vectors,'call',lambda self,m,p,b:(seen.append(b) or dict(result=dict(points=[]))))
    Vectors().query('collection','authorized-project',['allowed-point'],[0.0]*16,3)
    must=seen[0]['filter']['must']
    assert must==[dict(key='projectId',match=dict(value='authorized-project')),dict(has_id=['allowed-point'])]


def test_agent_claim_cannot_start_or_write_index(library):
    client,project,executor=library;source=text_source();configure()
    response=client.post('/api/knowledge/index',headers={'X-TestPilot-Project':project,'X-TestPilot-Claim':'agent-token'},json={})
    assert response.status_code==403
    identity=uid('TASK')
    from testpilot.job_queue import enqueue
    with session() as db:
        row=Record(id=identity,kind='task',status='queued',payload=dict(id=identity,status='queued',ownerId='service',executorId=executor['id'],messages=[]))
        db.add(row);enqueue(db,row,'agent')
    claim=executors.claim(executor['id'],'agent')
    with pytest.raises(PermissionError):rag.index_plan(claim['claim'])
    executors.finish(claim['claim'])


def test_provider_address_and_error_never_leak_key(monkeypatch):
    cfg=dict(baseUrl=PLAN_URL,model=MODEL)
    with pytest.raises(ValueError):rag.KnowledgeConfig(baseUrl='https://ark.cn-beijing.volces.com/api/v3').public()
    def failure(*args,**kwargs):
        assert args[0]==PLAN_URL+'/embeddings/multimodal'
        response=httpx.Response(401,text='private-key-secret',request=httpx.Request('POST',args[0]))
        response.raise_for_status()
    monkeypatch.setattr(httpx,'post',failure)
    with pytest.raises(ValueError) as error:embed(cfg,'private-key-secret','hello')
    assert 'private-key-secret' not in str(error.value) and '401' in str(error.value)


def test_chunk_positions_coverage_and_rank_fusion():
    text='# 登录规则\n'+('连续认证失败后冻结账户。\n'*100)
    pieces=split_text(text,500,60)
    assert pieces[0]['start']==0 and pieces[-1]['end']==len(text)
    assert all(c['text']==text[c['start']:c['end']] for c in pieces)
    assert all(a['end']>=b['start'] for a,b in zip(pieces,pieces[1:]))
    first=dict(chunkId='one',text='GET /api/ping',name='接口',heading='健康检查')
    second=dict(chunkId='two',text='修改用户',name='接口',heading='用户')
    assert lexical_rank([first,second],'/api/ping')[0]['chunkId']=='one'
    assert fuse([first,second],[first])[0]['chunkId']=='one'


def test_archive_during_embedding_rejects_stale_commit(library):
    client,project,executor=library;source=text_source();configure();action,token=begin(executor)
    version=rag.index_plan(token)['documents'][0]['hash']
    sources.archive_source(source,True)
    with pytest.raises(ValueError):rag.commit_document(token,source,version,[[1.0]*16],1)
    assert not rag.manifest().get('documents')
    rag.index_failure(token,source,'资料已归档');rag.finish_index(token);executors.finish(token)
    assert rag.status()['job']['failures'][0]['id']==source


def test_changed_configuration_requires_manual_index(library,monkeypatch):
    client,project,executor=library;source=text_source();cfg=configure();action,token=begin(executor)
    publish(token,source,cfg);rag.finish_index(token);executors.finish(token)
    rag.save_config(rag.KnowledgeConfig(chunkSize=900,overlap=80))
    monkeypatch.setattr(rag,'query_embedding',lambda *a:pytest.fail('old-model query'))
    assert rag.status()['pending']==1 and rag.status()['indexed']==0
    assert rag.search('冻结')['mode']=='keyword_fallback'
