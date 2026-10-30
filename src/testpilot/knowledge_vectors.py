"""项目资料的分段、词法召回和向量接口。原件仍由 sources 管理。"""
import hashlib
import math
import re
from collections import Counter
from uuid import NAMESPACE_URL, uuid5
import httpx
from .config import settings

PLAN_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
MODEL = "doubao-embedding-vision"

class EmbeddingError(ValueError):
    """供应商不可用时停止批量索引，避免对剩余资料重复发起失败请求。"""


def digest(value):
    """输入文字，返回 SHA-256 内容摘要；它用于检测变化，不是加密。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def split_text(text, size=1200, overlap=150):
    """按段落/行边界切分，长段落保留重叠；返回原文位置和章节，不改原文。"""
    if not 300 <= size <= 2400 or not 0 <= overlap < size // 2:
        raise ValueError("分段长度或重叠长度不合法")
    chunks=[];start=0;heading=""
    headings=[(m.start(),m.group(1).strip()) for m in re.finditer(r"(?m)^#{1,6}\s+(.+)$",text)]
    while start<len(text):
        end=min(start+size,len(text))
        if end<len(text):
            boundary=text.rfind("\n",start+size//2,end)
            if boundary>start:end=boundary+1
        for position,title in headings:
            if position<=start:heading=title
            else:break
        value=text[start:end]
        if value.strip():chunks.append(dict(start=start,end=end,heading=heading,text=value))
        if end==len(text):break
        start=max(start+1,end-overlap)
    return chunks


def point_id(project, source, version, fingerprint, start):
    """同一资料版本/模型/位置得到同一 UUID，索引重试可覆盖写入而不重复建点。"""
    return str(uuid5(NAMESPACE_URL,"testpilot:"+"|".join(map(str,[project,source,version,fingerprint,start]))))


def terms(text):
    """英文按词、中文按相邻双字切分；保留接口路径等精确串，供轻量词法检索。"""
    lowered=text.casefold()
    result=re.findall(r"[a-z0-9_./:-]+",lowered)
    for run in re.findall(r"[\u4e00-\u9fff]+",lowered):
        result.extend(run[i:i+2] for i in range(len(run)-1))
        if len(run)==1:result.append(run)
    return result


def lexical_rank(chunks, query):
    """根据词频、稀有度和长度做 BM25 排序；返回命中片段，不调用模型。"""
    if not chunks:return []
    bags=[Counter(terms(c["text"]+" "+c["name"]+" "+c["heading"])) for c in chunks]
    sizes=[sum(b.values()) for b in bags];average=sum(sizes)/len(sizes) or 1
    query_terms=set(terms(query));frequency={t:sum(t in b for b in bags) for t in query_terms}
    scores=[]
    for chunk,bag,length in zip(chunks,bags,sizes):
        score=0.0
        for term in query_terms:
            tf=bag[term]
            if tf:score+=math.log(1+(len(bags)-frequency[term]+.5)/(frequency[term]+.5))*tf*2.2/(tf+1.2*(.25+.75*length/average))
        if query.strip().casefold() in (chunk["text"]+chunk["name"]).casefold():score+=3
        if score:scores.append((score,chunk))
    return [c for _,c in sorted(scores,key=lambda v:v[0],reverse=True)]


def fuse(lexical, semantic):
    """RRF 合并两种排名；不直接相加尺度不同的 BM25 和向量相似度。"""
    scores={};values={}
    for group in (lexical,semantic):
        for rank,item in enumerate(group,1):
            identity=item["chunkId"];values[identity]=item
            scores[identity]=scores.get(identity,0)+1/(60+rank)
    return [values[k] for k in sorted(scores,key=scores.get,reverse=True)]


def embed(config, key, text):
    """执行端使用私有 Key 调用唯一 Plan 地址；返回有限数值向量与用量，错误不回显供应商正文。"""
    if config["baseUrl"]!=PLAN_URL or config["model"]!=MODEL:raise ValueError("向量模型必须使用已配置的火山 Plan 地址")
    if not key:raise ValueError("向量模型缺少 API Key，请在项目资料的 RAG 设置中配置")
    if not text.strip() or len(text)>2400:raise ValueError("向量输入必须为 1～2400 个字符")
    try:
        response=httpx.post(PLAN_URL+"/embeddings/multimodal",headers={"Authorization":"Bearer "+key},
            json=dict(model=MODEL,input=[dict(type="text",text=text)],encoding_format="float"),timeout=25,follow_redirects=False)
        response.raise_for_status()
        data=response.json();raw=data.get("data",{})
        vector=raw.get("embedding") if isinstance(raw,dict) else raw[0].get("embedding")
        if not isinstance(vector,list) or not 16<=len(vector)<=8192 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):
            raise ValueError("向量模型没有返回有效向量")
        return dict(vector=vector,dimension=len(vector),tokens=int(data.get("usage",{}).get("total_tokens",0)))
    except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError) as error:
        from .local_vault import safe_error
        raise EmbeddingError(safe_error(error)) from None


class Vectors:
    """仅平台持有 Qdrant 地址和访问凭据；执行器通过带 claim 的平台接口提交向量。"""
    def call(self,method,path,body=None):
        try:
            response=httpx.request(method,settings.qdrant_url.rstrip("/")+path,json=body,
                headers={"api-key":settings.qdrant_api_key} if settings.qdrant_api_key else {},timeout=20,follow_redirects=False)
            response.raise_for_status();return response.json()
        except (httpx.HTTPError,ValueError):raise ValueError("Qdrant 暂不可用，请检查知识库服务") from None

    @staticmethod
    def collection(fingerprint,dimension):
        return "testpilot_docs_"+fingerprint[:24]+"_"+str(dimension)

    def ensure(self,name,dimension):
        """按模型指纹建 Collection，并为强制项目过滤建立索引；已有集合不重复创建。"""
        collections=self.call("GET","/collections")["result"]["collections"]
        if any(c["name"]==name for c in collections):return
        try:self.call("PUT","/collections/"+name,dict(vectors=dict(size=dimension,distance="Cosine")))
        except ValueError:
            if not any(c["name"]==name for c in self.call("GET","/collections")["result"]["collections"]):raise
        self.call("PUT","/collections/"+name+"/index?wait=true",dict(field_name="projectId",field_schema="keyword"))

    def upsert(self,name,points):
        self.call("PUT","/collections/"+name+"/points?wait=true",dict(points=points))

    def existing(self,name,ids):
        if not ids:return False
        try:return len(self.call("POST","/collections/"+name+"/points",dict(ids=ids,with_payload=False,with_vector=False))["result"])==len(ids)
        except ValueError:return False

    def query(self,name,project,ids,vector,limit):
        # 项目过滤来自后端身份；has_id 只允许已正式发布的片段，未完成/旧版索引不会混进搜索。
        if not ids:return []
        result=self.call("POST","/collections/"+name+"/points/query",dict(query=vector,limit=limit,with_payload=True,
            filter=dict(must=[dict(key="projectId",match=dict(value=project)),dict(has_id=ids)])))
        return result["result"]["points"]

    def delete(self,name,ids):
        if ids:self.call("POST","/collections/"+name+"/points/delete?wait=true",dict(points=ids))
