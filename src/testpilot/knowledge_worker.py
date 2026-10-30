"""资料索引工作进程：调用执行端私有凭据，通过平台回传向量，不直连 MySQL/Qdrant。"""
from .replica import request
from .local_vault import model_vault
from .knowledge_vectors import split_text,embed,digest,EmbeddingError


def process(source,cfg,key,budget):
    """下载一份授权原文、检查版本并向量化；返回消耗字符数，进度由平台保存。"""
    if source["reuse"]:
        request("POST","/api/knowledge/index/document",json=dict(source_id=source["id"],version=source["hash"],vectors=[],tokens=0,reused=True))
        return 0
    parts=[];offset=0;characters=0
    while True:
        page=request("GET","/api/sources/"+source["id"]+"/text",params=dict(offset=offset,limit=16000)).json()
        parts.append(page["content"]);characters+=len(page["content"])
        if characters>budget:raise ValueError("资料正文超过本次索引字数上限")
        offset=page["nextOffset"]
        if offset is None:break
    text="".join(parts)
    if digest(text)!=source["hash"]:raise ValueError("资料正文发生变化，请重新建立索引")
    chunks=split_text(text,cfg["chunkSize"],cfg["overlap"])
    if not chunks or len(chunks)>1000:raise ValueError("资料没有可索引正文或分段过多")
    vectors=[];tokens=0
    for chunk in chunks:
        # 取消/失去租约后，平台拒绝检查请求，不再向供应商发送下一段。
        request("GET","/api/knowledge/index/check")
        result=embed(cfg,key,chunk["text"]);vectors.append(result["vector"]);tokens+=result["tokens"]
    request("POST","/api/knowledge/index/document",json=dict(source_id=source["id"],version=source["hash"],vectors=vectors,tokens=tokens,reused=False))
    return characters


def execute():
    """读取按钮提交的索引计划；正常片段发布到 Qdrant，失败资料记录说明，不调用聊天模型。"""
    plan=request("GET","/api/knowledge/index/plan").json();cfg=plan["config"]
    needs_key=any(not s["reuse"] for s in plan["documents"])
    key=model_vault(cfg["ownerId"]).for_profile(cfg) if needs_key else ""
    if needs_key and not key:raise ValueError("向量模型缺少 API Key，请在项目 RAG 设置中填写")
    for source in plan.get("skipped",[]):
        request("POST","/api/knowledge/index/failure",json=dict(source_id=source["id"],message=source["message"]))
    characters=0
    for source in plan["documents"]:
        try:characters+=process(source,cfg,key,cfg["maxCharacters"]-characters)
        except ValueError as error:
            # 只记录本模块/向量适配器已清理的错误；HTTP 同步异常直接中止，不输出请求凭据。
            request("POST","/api/knowledge/index/failure",json=dict(source_id=source["id"],message=str(error)[:300]))
            if isinstance(error,EmbeddingError):raise
    request("POST","/api/knowledge/index/finish",json={})
