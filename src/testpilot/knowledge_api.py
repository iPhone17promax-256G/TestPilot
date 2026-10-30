"""网页 RAG 控制与索引进程回传接口。没有向 Agent 注册建库工具。"""
from fastapi import APIRouter,Header,Request,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from . import knowledge as rag
from .storage import session

router=APIRouter(prefix="/api/knowledge")

@router.get("")
def status():return rag.status()

@router.post("/config")
def configure(body:rag.KnowledgeConfig):return rag.save_config(body)

@router.post("/index",status_code=202)
def start(request:Request):
    if request.headers.get("x-testpilot-claim"):raise HTTPException(403,"索引只能由用户在项目资料中手动建立")
    return rag.start_index()

@router.post("/cancel")
def cancel():return rag.cancel_index()

class Search(BaseModel):
    model_config=ConfigDict(extra="forbid")
    query:str=Field(default="",max_length=200)
    mode:str="auto"
    offset:int=Field(default=0,ge=0,le=1000)
    limit:int=Field(default=12,ge=1,le=50)

@router.post("/search")
def search(body:Search):return rag.search(**body.model_dump())

@router.get("/index/plan")
def plan(x_testpilot_claim:str=Header()):return rag.index_plan(x_testpilot_claim)

@router.get("/index/check")
def check(x_testpilot_claim:str=Header()):
    with session() as db:rag.authorized_index(db,x_testpilot_claim)
    return {"status":"ok"}

class Document(BaseModel):
    model_config=ConfigDict(extra="forbid")
    source_id:str
    version:str=Field(max_length=64)
    vectors:list[list[float]]=Field(max_length=1000)
    tokens:int=Field(default=0,ge=0,le=10000000)
    reused:bool=False

@router.post("/index/document")
def document(body:Document,x_testpilot_claim:str=Header()):return rag.commit_document(x_testpilot_claim,**body.model_dump())

@router.post("/index/finish")
def finish(x_testpilot_claim:str=Header()):return rag.finish_index(x_testpilot_claim)

class Failure(BaseModel):
    source_id:str
    message:str=Field(max_length=300)

@router.post("/index/failure")
def failure(body:Failure,x_testpilot_claim:str=Header()):return rag.index_failure(x_testpilot_claim,body.source_id,body.message)
