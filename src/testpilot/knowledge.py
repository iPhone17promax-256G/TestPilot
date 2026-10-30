"""手动、按项目建立资料索引。Agent 只有检索入口，不能建立/更新索引。"""
import json
import math
from sqlalchemy import select
from pydantic import BaseModel,ConfigDict,Field
from .storage import Record,session,uid,read_record,now
from .workspace import current_project,current_user
from .knowledge_vectors import PLAN_URL,MODEL,Vectors,digest,split_text,point_id,lexical_rank,fuse


class KnowledgeConfig(BaseModel):
    model_config=ConfigDict(extra="forbid")
    baseUrl:str=PLAN_URL
    model:str=MODEL
    chunkSize:int=Field(default=1200,ge=300,le=2400)
    overlap:int=Field(default=150,ge=0,le=500)
    maxCharacters:int=Field(default=200000,ge=1000,le=1000000)

    def public(self):
        """限制套餐地址和分段参数；配置里不接受 API Key 或任意请求头。"""
        if self.baseUrl!=PLAN_URL or self.model!=MODEL:raise ValueError("请使用火山 Plan 专属地址和 doubao-embedding-vision，不能切换至按量地址")
        if self.overlap>=self.chunkSize//2:raise ValueError("重叠长度必须小于分段长度的一半")
        return self.model_dump()


def identity(kind):
    """每个项目一份配置/索引清单，ID 不由浏览器自报。"""
    return kind+"-"+digest(current_project())[:12].upper()


def config():
    with session() as db:
        row=db.get(Record,identity("RAG"))
        return dict(row.payload) if row and row.project_id==current_project() else None


def can_configure():
    """项目负责人、本地身份或管理员可授权向量凭据；普通成员可使用已授权的资料索引。"""
    from .authentication import user_by_id
    from .config import settings
    project=read_record(current_project(),"project")
    user=user_by_id(current_user()) or {}
    return settings.deployment_mode=="local" or project.get("ownerId")==current_user() or user.get("role")=="admin"


def save_config(body):
    """保存项目不含密钥的向量配置；指纹变化使旧模型索引失效，不自动发起重新索引。"""
    if not can_configure():raise PermissionError("只有项目负责人或管理员可以配置 RAG")
    public=body.public()
    fingerprint=digest(json.dumps({k:public[k] for k in ["baseUrl","model","chunkSize","overlap"]},sort_keys=True)+":chunks-v1")
    with session() as db:
        row=db.get(Record,identity("RAG"))
        payload=dict(public,id=identity("RAG"),ownerId=current_user(),projectId=current_project(),fingerprint=fingerprint)
        if row:row.payload=payload
        else:db.add(Record(id=payload["id"],kind="rag_config",project_id=current_project(),payload=payload))
    return payload


def manifest():
    with session() as db:
        row=db.get(Record,identity("INDEX"))
        return dict(row.payload) if row else dict(documents={})


_hash_cache={}
def source_hash(source):
    """读取原正文计算版本；历史文件用 mtime 缓存摘要，缓存不含正文或凭据。"""
    from .sources import source_text,file_path
    if source.get("textHash"):return source["textHash"]
    if source.get("storage")=="file":
        stat=file_path(source,True).stat();key=(source["id"],stat.st_mtime_ns,stat.st_size)
        if key in _hash_cache:return _hash_cache[key]
        value=digest(source_text(source))
        if len(_hash_cache)>256:_hash_cache.clear()
        _hash_cache[key]=value;return value
    return digest(source_text(source))


def documents():
    """只取当前项目未归档且可读的 source；不读取聊天附件、任务、用例、报告或 Skill。"""
    from .sources import source_records
    return [s for s in source_records() if not s.get("archived") and s.get("readable",True)]


def latest_action():
    with session() as db:
        cfg=db.get(Record,identity("RAG"))
        selected=db.get(Record,cfg.payload.get("lastActionId","")) if cfg else None
        if selected and selected.project_id==current_project() and selected.kind=="local_action" and selected.payload.get("name")=="_rag_index":return dict(selected.payload)
        rows=db.scalars(select(Record).where(Record.kind=="local_action",Record.project_id==current_project()).order_by(Record.created_at.desc()))
        return next((dict(r.payload) for r in rows if r.payload.get("name")=="_rag_index"),None)


def status():
    """网页状态：有效片段数量、待更新资料和共享任务进度；访问本函数不会调用向量模型。"""
    from .sources import source_records
    cfg=config();index=manifest();rows=documents();active={s["id"]:s for s in rows}
    valid={k:v for k,v in index.get("documents",{}).items() if k in active and cfg and v["fingerprint"]==cfg["fingerprint"] and v["hash"]==source_hash(active[k])}
    action=latest_action()
    progress={k:action.get(k) for k in ["id","status","summary","processed","total","indexed","reused","tokens","failures"]} if action else None
    return dict(config=cfg,canConfigure=can_configure(),built=bool(index.get("builtAt")),indexed=len(valid),
        chunks=sum(v["chunks"] for v in valid.values()),pending=len(rows)-len(valid),
        characters=sum(s.get("characters",len(s.get("content",""))) for s in rows),
        unreadable=sum(not s.get("readable",True) for s in source_records() if not s.get("archived")),
        documents={k:dict(chunks=v["chunks"],hash=v["hash"]) for k,v in valid.items()},job=progress)


def start_index():
    """仅网页按钮调用：快照本次全部可读项目资料，创建 Redis local Job，不在 API 请求里向量化。"""
    from .executors import selected_executor
    from .job_queue import enqueue
    cfg=config()
    if not cfg:raise ValueError("请先配置项目资料的向量模型")
    rows=documents()
    if not rows and not manifest().get("builtAt"):raise ValueError("当前项目没有可解析的资料")
    size=sum(s.get("characters",len(s.get("content",""))) for s in rows)
    if size>cfg["maxCharacters"]:raise ValueError("资料超过本次索引字数上限，请调整 RAG 设置后再建立索引")
    executor=selected_executor()
    if not executor:raise ValueError("请先连接执行服务")
    with session() as db:
        # 锁项目保证两个按钮请求不会同时新建索引工作。
        db.scalar(select(Record).where(Record.id==current_project()).with_for_update())
        active=db.scalars(select(Record).where(Record.kind=="local_action",Record.project_id==current_project(),Record.status.in_(["queued","running"]))).all()
        prior=next((r for r in active if r.payload.get("name")=="_rag_index"),None)
        if prior:return prior.payload
        payload=dict(id=uid("ACTION"),ownerId=current_user(),executorId=executor,status="queued",name="_rag_index",createdAt=now().isoformat(),
            arguments=dict(fingerprint=cfg["fingerprint"],sources=[dict(id=s["id"],hash=source_hash(s)) for s in rows]),
            summary="等待执行服务建立项目资料索引",processed=0,total=len(rows),indexed=0,reused=0,tokens=0,failures=[])
        row=Record(id=payload["id"],kind="local_action",project_id=current_project(),status="queued",payload=payload)
        db.add(row);enqueue(db,row,"local")
        cfg_row=db.get(Record,identity("RAG"));cfg_row.payload=dict(cfg_row.payload,lastActionId=row.id)
    return row.payload


def cancel_index():
    """取消当前项目索引；已成功发布的片段保留，执行器在心跳检查时停止剩余工作。"""
    from .storage import update_record
    action=latest_action()
    if action and action["status"] in {"queued","running"}:
        with session() as db:
            row=db.scalar(select(Record).where(Record.id==action["id"]).with_for_update())
            row.status="cancelled";row.payload=dict(row.payload,status="cancelled",summary="索引已取消，已完成资料仍可检索")
    return status()


def authorized_index(db,token):
    """平台重新验证执行租约和作业种类；普通 Agent claim 不能调用索引写接口。"""
    from .executors import authorized_claim
    job,row=authorized_claim(db,token)
    if row.kind!="local_action" or row.payload.get("name")!="_rag_index" or row.status!="running":raise PermissionError("当前作业没有建立资料索引的权限")
    cfg=config()
    if not cfg or row.payload["arguments"]["fingerprint"]!=cfg["fingerprint"]:raise ValueError("向量配置已变化，请重新建立索引")
    return row,cfg


def index_plan(token):
    """下发本次按钮选定的资料及复用清单；正文另用已有 sources_read 分页读取。"""
    with session() as db:
        row,cfg=authorized_index(db,token);old=manifest();available={s["id"]:s for s in documents()};plan=[];skipped=[]
        for item in row.payload["arguments"]["sources"]:
            source=available.get(item["id"])
            if not source or source_hash(source)!=item["hash"]:
                skipped.append(dict(id=item["id"],message="资料在索引前已归档或变化，本次跳过"));continue
            cached=old.get("documents",{}).get(source["id"],{})
            reuse=cached.get("hash")==item["hash"] and cached.get("fingerprint")==cfg["fingerprint"]
            if reuse:reuse=Vectors().existing(cached["collection"],cached["pointIds"])
            plan.append(dict(id=source["id"],name=source["name"],hash=item["hash"],reuse=bool(reuse)))
        return dict(config=cfg,projectId=current_project(),documents=plan,skipped=skipped)


def commit_document(token,source_id,version,vectors,tokens,reused=False):
    """只发布当前原文的完整一份索引：Qdrant 写成功后更新 MySQL 清单，未提交点不会被检索。"""
    from .sources import source_text
    with session() as db:
        action,cfg=authorized_index(db,token)
        expected=next((s for s in action.payload["arguments"]["sources"] if s["id"]==source_id),None)
        source=read_record(source_id,"source")
        if not expected or source.get("archived") or expected["hash"]!=version or source_hash(source)!=version:raise ValueError("资料已变化或归档，不发布旧版本索引")
        index=db.scalar(select(Record).where(Record.id==identity("INDEX")).with_for_update())
        if not index:index=Record(id=identity("INDEX"),kind="rag_index",project_id=current_project(),payload=dict(documents={}));db.add(index)
        old=index.payload.get("documents",{}).get(source_id,{})
        if reused:
            if old.get("hash")!=version or old.get("fingerprint")!=cfg["fingerprint"] or not Vectors().existing(old["collection"],old["pointIds"]):raise ValueError("已有索引不完整，请重新索引")
        else:
            chunks=split_text(source_text(source),cfg["chunkSize"],cfg["overlap"])
            if len(chunks)>1000 or len(chunks)!=len(vectors) or not vectors:raise ValueError("资料片段和向量数量不匹配")
            dimension=len(vectors[0])
            if not 16<=dimension<=8192 or any(len(v)!=dimension or any(not math.isfinite(n) for n in v) for v in vectors):raise ValueError("向量维度或数值不合法")
            collection=Vectors.collection(cfg["fingerprint"],dimension);points=[]
            for chunk,vector in zip(chunks,vectors):
                pid=point_id(current_project(),source_id,version,cfg["fingerprint"],chunk["start"])
                points.append(dict(id=pid,vector=vector,payload=dict(projectId=current_project(),sourceId=source_id,hash=version,start=chunk["start"],end=chunk["end"],heading=chunk["heading"])))
            store=Vectors();store.ensure(collection,dimension);store.upsert(collection,points)
            entry=dict(hash=version,fingerprint=cfg["fingerprint"],collection=collection,dimension=dimension,chunks=len(points),pointIds=[p["id"] for p in points])
            index.payload=dict(index.payload,documents={**index.payload.get("documents",{}),source_id:entry},builtAt=now().isoformat())
            # 清单先只引用新点；删除失败也不会把旧点再次返回给模型。
            if old.get("collection"):
                try:store.delete(old["collection"],list(set(old.get("pointIds",[]))-set(entry["pointIds"])))
                except ValueError:pass
        completed=list(action.payload.get("completedSources",[]))
        if source_id not in completed:
            completed.append(source_id)
            action.payload=dict(action.payload,completedSources=completed,processed=len(completed),
                reused=action.payload.get("reused",0)+int(reused),indexed=action.payload.get("indexed",0)+int(not reused),
                tokens=action.payload.get("tokens",0)+tokens,summary=f"已处理 {len(completed)} / {action.payload['total']} 份资料")
    return {"status":"ok"}


def finish_index(token):
    """正常收尾并移除归档/不存在资料的索引；不自动纳入按钮点击后新上传的资料。"""
    with session() as db:
        action,cfg=authorized_index(db,token);active={s["id"] for s in documents()}
        index=db.get(Record,identity("INDEX"))
        if index:
            kept={k:v for k,v in index.payload.get("documents",{}).items() if k in active}
            for source,entry in index.payload.get("documents",{}).items():
                if source not in active:
                    try:Vectors().delete(entry["collection"],entry["pointIds"])
                    except ValueError:pass
            index.payload=dict(index.payload,documents=kept,builtAt=now().isoformat())
        failed=len(action.payload.get("failures",[]))
        action.status="completed";action.payload=dict(action.payload,status="completed",summary=f"索引完成：新建 {action.payload.get('indexed',0)} 份，复用 {action.payload.get('reused',0)} 份，跳过/失败 {failed} 份")
    return {"status":"completed"}


def index_failure(token,source_id,message):
    """记录有限的资料失败说明；不接收任意项目文件或供应商响应正文。"""
    with session() as db:
        action,cfg=authorized_index(db,token)
        if source_id not in {s["id"] for s in action.payload["arguments"]["sources"]}:raise PermissionError("资料不属于本次索引")
        completed=list(action.payload.get("completedSources",[]))
        if source_id not in completed:
            completed.append(source_id)
            source=db.get(Record,source_id)
            action.payload=dict(action.payload,completedSources=completed,processed=len(completed),
                failures=[*action.payload.get("failures",[]),dict(id=source_id,name=source.payload.get("name",source_id) if source else source_id,message=message[:300])])
    return {"status":"ok"}


def query_embedding(cfg,text):
    """平台向固定执行服务请求查询向量；Key 留在执行端，不经平台模型配置存储。"""
    import httpx
    from .config import settings,require_https_url
    if not settings.managed_executor:raise ValueError("当前执行服务不支持共享 RAG 查询，请使用自动执行服务")
    # 这是平台向执行器发出的带内部凭据的请求；与向量模型地址、被测目标的协议选择无关。
    runtime_url=require_https_url(settings.runtime_url,"向量查询执行服务地址")
    try:
        response=httpx.post(runtime_url+"/knowledge/embed",json=dict(projectId=current_project(),text=text),
            headers={"Authorization":"Bearer "+settings.managed_token,"X-TestPilot-Managed-Account":current_user()},timeout=30,follow_redirects=False)
        if response.status_code>=400:raise ValueError("查询向量不可用，请检查项目的向量密钥或额度")
        return response.json()["vector"]
    except (httpx.HTTPError,KeyError):raise ValueError("执行服务暂不可用，已回退关键词检索") from None


def search(query,offset=0,limit=12,mode="auto"):
    """只对已有索引做查询。未建库时保持旧关键词查询，失败回退并明确标记原因。"""
    from .sources import keyword_search_sources,source_text
    if mode not in {"auto","keyword","semantic","hybrid"}:raise ValueError("资料检索模式不正确")
    if not 0<=offset<=1000 or not 1<=limit<=50 or len(query)>200:raise ValueError("检索范围不合法")
    cfg=config();index=manifest()
    if not query.strip() or mode=="keyword" or not cfg or not index.get("builtAt"):
        return dict(items=keyword_search_sources(query,offset,limit),mode="keyword",warning="" if mode in {"auto","keyword"} else "尚未建立 RAG 索引，使用关键词检索")
    chunks=[];allowed={};by_id={}
    for source in documents():
        version=source_hash(source);entry=index.get("documents",{}).get(source["id"],{})
        for piece in split_text(source_text(source),cfg["chunkSize"],cfg["overlap"]):
            pid=point_id(current_project(),source["id"],version,cfg["fingerprint"],piece["start"])
            chunk=dict(id=source["id"],name=source["name"],chunkId=pid,version=version,readable=True,type=source.get("type","text"),
                characters=source.get("characters",0),warnings=source.get("warnings",[]),**piece)
            chunks.append(chunk);by_id[pid]=chunk
        if entry.get("hash")==version and entry.get("fingerprint")==cfg["fingerprint"]:
            allowed.setdefault(entry["collection"],[]).extend(entry["pointIds"])
    lexical=lexical_rank(chunks,query);semantic=[];warning="";actual="hybrid" if mode=="auto" else mode
    if allowed:
        try:
            vector=query_embedding(cfg,query)
            for collection,ids in allowed.items():
                for hit in Vectors().query(collection,current_project(),ids,vector,min(100,offset+limit+20)):
                    if str(hit["id"]) in by_id:semantic.append(by_id[str(hit["id"])])
        except ValueError as error:warning=str(error);actual="keyword_fallback"
    else:warning="索引与当前资料或向量配置不一致，请点击更新索引";actual="keyword_fallback"
    ranked=semantic if actual=="semantic" else lexical if actual=="keyword_fallback" else fuse(lexical,semantic)
    items=[]
    for hit in ranked[offset:offset+limit]:
        item={k:hit[k] for k in ["id","name","chunkId","version","readable","type","characters","warnings","start","end","heading"]}
        item.update(summary=hit["text"][:1000],retrievalMode=actual)
        if warning:item["retrievalWarning"]=warning
        items.append(item)
    return dict(items=items,mode=actual,warning=warning)
