"""Redis is the authority for Job scheduling; SQL only holds transactional delivery events.

Streams hold queued deliveries, hashes hold job ownership/state, and a sorted set indexes
lease deadlines. Executors never connect to Redis: authenticated platform endpoints do.
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import redis
import redis.asyncio as async_redis
from sqlalchemy import select, delete, inspect, text

from .config import settings
from .workspace import current_user, current_project

log = logging.getLogger(__name__)
_wake = threading.Event()


@dataclass
class QueueJob:
    """一次调度的数据快照，不是 ORM 表。一次 Task 续聊会生成新的 Job ID。"""
    id: str
    kind: str
    record_id: str
    account_id: str
    project_id: str
    executor_id: str = ""
    status: str = "queued"
    claim: str = ""
    lease: datetime | None = None
    stream: str = ""
    message_id: str = ""


# 这些脚本在 Redis 内一次完成检查与更新，不能先 GET 后 SET，否则多个 API 进程会抢到同一份工作。
# enqueue 以 Job ID 去重；outbox 重发同一事件不会再 XADD 一条消息。
_PUBLISH = """
local op = ARGV[1]
if op == 'enqueue' or op == 'import' then
  if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
  redis.call('HSET', KEYS[1], 'data', ARGV[2], 'status', ARGV[3], 'claim', '', 'lease', '0', 'stream', KEYS[3])
  redis.call('SET', KEYS[2], ARGV[4])
  if ARGV[3] == 'queued' then
    local mid = redis.call('XADD', KEYS[3], '*', 'jobId', ARGV[4])
    redis.call('HSET', KEYS[1], 'messageId', mid)
  else
    redis.call('EXPIRE', KEYS[1], ARGV[6]); redis.call('EXPIRE', KEYS[2], ARGV[6])
  end
  return 1
end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[5] then return 0 end
if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
local mid = redis.call('HGET', KEYS[1], 'messageId')
local stream = redis.call('HGET', KEYS[1], 'stream')
if mid and stream then redis.call('XACK', stream, 'workers', mid); redis.call('XDEL', stream, mid) end
redis.call('HSET', KEYS[1], 'status', ARGV[3], 'claim', '', 'lease', '0')
redis.call('ZREM', KEYS[4], ARGV[4]); redis.call('DEL', KEYS[5])
redis.call('EXPIRE', KEYS[1], ARGV[6])
if redis.call('GET', KEYS[2]) == ARGV[4] then redis.call('EXPIRE', KEYS[2], ARGV[6]) end
return 1
"""

_TAKE = """
if redis.call('HGET', KEYS[1], 'status') ~= 'queued' then return 0 end
local data = cjson.decode(redis.call('HGET', KEYS[1], 'data'))
if data.account_id ~= ARGV[1] then return 0 end
if data.executor_id ~= '' and data.executor_id ~= ARGV[2] then return 0 end
data.executor_id = ARGV[2]
redis.call('HSET', KEYS[1], 'data', cjson.encode(data), 'status', 'running', 'claim', ARGV[3], 'lease', ARGV[4])
redis.call('SET', KEYS[2], data.id, 'EX', ARGV[5])
redis.call('ZADD', KEYS[3], ARGV[4], data.id)
return 1
"""

_RENEW = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running' then return 0 end
if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return 0 end
if tonumber(redis.call('HGET', KEYS[1], 'lease') or '0') <= tonumber(ARGV[2]) then return 0 end
redis.call('HSET', KEYS[1], 'lease', ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[5])
return 1
"""


@lru_cache(maxsize=4)
def connection(url):
    # 连接池复用；Redis 错误不拼出带密码的 URL。阻塞领取单独设定足够的读取超时。
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2,
                                socket_timeout=35, health_check_interval=30)


class RedisJobs:
    """输入是可信平台核验后的账号/执行器；输出为 QueueJob，不携带模型密钥。"""
    def __init__(self, client=None, prefix=None):
        self.client = client or connection(settings.redis_url)
        self.prefix = prefix or settings.redis_prefix

    def key(self, category, identity=""):
        return f"{self.prefix}:{category}" + (":" + identity if identity else "")

    def stream(self, account, executor, kind):
        route = hashlib.sha256(f"{account}\0{executor}\0{kind}".encode()).hexdigest()
        return self.key("queue", route)

    def get(self, identity):
        if not identity: return None
        fields = self.client.hgetall(self.key("job", identity))
        return self.decode(fields)

    @staticmethod
    def decode(fields):
        if not fields: return None
        job = QueueJob(**json.loads(fields["data"]))
        job.status, job.claim = fields["status"], fields.get("claim", "")
        seconds = float(fields.get("lease", 0))
        job.lease = datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None) if seconds else None
        job.stream, job.message_id = fields.get("stream", ""), fields.get("messageId", "")
        return job

    def for_records(self, identities):
        identities=list(identities)
        if not identities:return []
        # 授权快照批量取队列信息，两次 Redis 往返；不对每条业务记录重复 GET/HGETALL。
        with self.client.pipeline(transaction=False) as pipe:
            for identity in identities:pipe.get(self.key("record",identity))
            ids=[identity for identity in pipe.execute() if identity]
        if not ids:return []
        with self.client.pipeline(transaction=False) as pipe:
            for identity in ids:pipe.hgetall(self.key("job",identity))
            return [job for fields in pipe.execute() if (job:=self.decode(fields))]

    def for_record(self, identity):
        return self.get(self.client.get(self.key("record", identity)))

    def for_claim(self, token):
        job = self.get(self.client.get(self.key("claim", token)))
        if not job or job.claim != token or job.status != "running" or not job.lease or job.lease <= datetime.now(timezone.utc).replace(tzinfo=None):
            raise PermissionError("执行租约无效或已经过期")
        return job

    def publish(self, event):
        job = event["job"]
        token = event.get("claim", "")
        stream = self.stream(job["account_id"], job.get("executor_id", ""), job["kind"])
        self.client.eval(_PUBLISH, 5, self.key("job", job["id"]), self.key("record", job["record_id"]),
                         stream, self.key("leases"), self.key("claim", token),
                         event["op"], json.dumps(job), event.get("status", "queued"),
                         job["id"], token, settings.job_history_seconds)

    def claim(self, account, executor, kind, wait_seconds=0):
        # 指定执行器的队列与账号共享队列分开路由；不能让其他账号先读消息再由 Python 过滤。
        streams = list(dict.fromkeys([self.stream(account, executor, kind), self.stream(account, "", kind)]))
        for stream in streams:
            try: self.client.xgroup_create(stream, "workers", id="0", mkstream=True)
            except redis.ResponseError as error:
                if "BUSYGROUP" not in str(error): raise
        consumer = hashlib.sha256(executor.encode()).hexdigest()
        # API 在读到消息后崩溃、尚未写 running 的消息仍在 PEL。相同执行器重连时先读自己的未确认消息。
        pending = self.client.xreadgroup("workers", consumer, {s: "0" for s in streams}, count=64)
        job = self._take(pending, account, executor)
        if job: return job
        messages = self.client.xreadgroup("workers", consumer, {s: ">" for s in streams}, count=1,
                                         block=int(wait_seconds*1000) if wait_seconds else None)
        job=self._take(messages, account, executor)
        if job:return job
        # 共享队列的通知可能被已断开的 API 消费者读走，但尚未签发执行租约。
        # 只转移仍 queued 的消息；running 工作的恢复交给租约，不因 Stream 重投就重复执行。
        for stream in streams:
            for message_id,item in self.client.xrange(stream,count=64):
                if self.client.hget(self.key("job",item["jobId"]),"status")!="queued":continue
                recovered=self.client.xclaim(stream,"workers",consumer,min_idle_time=0,message_ids=[message_id])
                job=self._take([(stream,recovered)],account,executor)
                if job:return job
        return None

    def _take(self, messages, account, executor):
        for stream, entries in messages:
            for message_id, values in entries:
                identity = values.get("jobId", "")
                token = secrets.token_urlsafe(32)
                deadline = time.time() + settings.job_lease_seconds
                accepted = self.client.eval(_TAKE, 3, self.key("job", identity), self.key("claim", token),
                                            self.key("leases"), account, executor, token, deadline,
                                            settings.job_lease_seconds + 45)
                if accepted: return self.get(identity)
                if not self.client.exists(self.key("job", identity)):
                    self.client.xack(stream, "workers", message_id)
                    self.client.xdel(stream, message_id)
        return None

    def renew(self, job):
        accepted = self.client.eval(_RENEW, 3, self.key("job", job.id), self.key("claim", job.claim),
                                    self.key("leases"), job.claim, time.time(),
                                    time.time()+settings.job_lease_seconds, job.id, settings.job_lease_seconds+45)
        if not accepted: raise PermissionError("执行租约失效，不能继续操作")

    def expired(self):
        ids = self.client.zrangebyscore(self.key("leases"), "-inf", time.time(), start=0, num=100)
        return [j for identity in ids if (j := self.get(identity)) and j.status == "running" and j.lease and j.lease <= datetime.now(timezone.utc).replace(tzinfo=None)]


async def wait_available(account, executor, kind, seconds=25):
    """等待队列可领取，不提前签发租约。实际 claim 由空闲执行槽发起，避免占满资源时抢走 Job。"""
    queue=RedisJobs()
    streams=list(dict.fromkeys([queue.stream(account,executor,kind),queue.stream(account,"",kind)]))
    client=async_redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_connect_timeout=2,socket_timeout=35)
    try:
        last={}
        for stream in streams:
            try:await client.xgroup_create(stream,"workers",id="0",mkstream=True)
            except redis.ResponseError as error:
                if "BUSYGROUP" not in str(error):raise
            # 先记住流末尾，再检查排队状态；检查与开始等待之间新来的消息不会漏掉。
            last[stream]=(await client.xinfo_stream(stream))["last-generated-id"]
            entries=await client.xrange(stream,count=64)
            if entries:
                async with client.pipeline(transaction=False) as pipe:
                    for _,item in entries:pipe.hget(queue.key("job",item["jobId"]),"status")
                    if "queued" in await pipe.execute():return True
        return bool(await client.xread(last,block=int(seconds*1000),count=1))
    finally:await client.aclose()


def get_job(db, record_id):
    """平台读 Redis；执行子进程只读授权快照中的调度信息，不连接 Redis。"""
    if settings.role == "executor" and settings.executor_claim:
        raw = next((j for j in db.info.get("jobs", []) if j["record_id"] == record_id), None)
        if not raw: return None
        return QueueJob(**{k: raw[k] for k in ["id", "kind", "record_id", "account_id", "project_id", "executor_id", "status", "claim"]},
                        lease=datetime.fromisoformat(raw["lease"]) if raw.get("lease") else None)
    return RedisJobs().for_record(record_id)


def add_event(db, event):
    """与业务记录同事务写待投递事件；事务回滚就不会向 Redis 派发不存在的 Run。"""
    from .storage import DispatchEvent, uid
    identity = event["job"]["id"] if event["op"] in {"enqueue", "import"} else uid("DELIVERY")
    db.add(DispatchEvent(id=identity, record_id=event["job"]["record_id"], payload=event))
    db.info.setdefault("dispatch_ids", []).append(identity)


def enqueue(db, row, kind):
    """准备新一轮调度。输入业务 Record；修改 dispatchId，并登记 outbox；不会提前执行。"""
    if settings.role == "executor" and settings.executor_claim:
        # Agent 新建 Run 只同步业务记录；平台 commit 在确认归属后负责创建真正的 Redis Job。
        return
    from .storage import uid
    identity = uid("JOB")
    row.payload = dict(row.payload, dispatchId=identity)
    data = dict(id=identity, kind=kind, record_id=row.id,
                account_id=row.payload.get("ownerId") or current_user(),
                project_id=row.project_id or current_project(), executor_id=row.payload.get("executorId", ""))
    add_event(db, dict(op="enqueue", job=data))


def complete(db, job, status="finished"):
    data = {key: getattr(job, key) for key in ["id", "kind", "record_id", "account_id", "project_id", "executor_id"]}
    add_event(db, dict(op="finish", job=data, claim=job.claim, status=status))


def deliver_committed(identities):
    # 快路径只投递本次已提交的事件。Redis 暂时不可用时不撤销业务数据，后台稍后重试。
    try: flush_outbox(identities)
    except (redis.RedisError, OSError):
        log.warning("队列暂不可用，已提交的调度事件等待重试")
        _wake.set()


def flush_outbox(identities=None):
    """只锁待投递事件，不扫描 jobs。Redis 已接收但 SQL 确认失败时重发同一 ID。"""
    from .storage import Session, DispatchEvent, now
    with Session.begin() as db:
        query = select(DispatchEvent).where(DispatchEvent.delivered_at.is_(None))
        if identities is not None: query = query.where(DispatchEvent.id.in_(identities))
        events = db.scalars(query.order_by(DispatchEvent.created_at).with_for_update(skip_locked=True).limit(100)).all()
        for event in events:
            RedisJobs().publish(event.payload)
            event.delivered_at = now()
        return len(events)


def migrate_sql_jobs():
    """一次性迁移旧 jobs 表：保存私有备份，导入事件，废止在途领取，最后删除旧表。"""
    from .storage import engine, Session, Record, DispatchEvent, now
    if "jobs" not in inspect(engine).get_table_names(): return 0
    with engine.connect() as connection:
        old = [dict(r) for r in connection.execute(text("SELECT * FROM jobs")).mappings()]
    backup = settings.data / "migrations" / "jobs-before-redis.json"
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        import os
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file: json.dump(old, file, default=str)
    with Session.begin() as db:
        for original in old:
            row = db.get(Record, original["record_id"])
            if not row: continue
            identity = original["id"]
            if db.get(DispatchEvent, identity): continue
            status = "interrupted" if original["status"] == "running" else original["status"]
            if row.status == "cancelled": status = "cancelled"
            row.payload = dict(row.payload, dispatchId=identity)
            if original["status"] == "running" and row.status == "running":
                row.status = "interrupted"
                row.payload = dict(row.payload, status="interrupted", summary="调度迁移后请确认实际结果，再继续")
            data = dict(id=identity, kind=original["kind"], record_id=row.id,
                        account_id=row.payload.get("ownerId") or "service", project_id=row.project_id,
                        executor_id=row.payload.get("executorId", ""))
            db.add(DispatchEvent(id=identity, record_id=row.id, payload=dict(op="import", job=data, status=status)))
    # 旧表的数据已在 outbox 中持久化；队列暂离线也可以恢复投递，不再保留双重调度来源。
    with engine.begin() as connection: connection.execute(text("DROP TABLE jobs"))
    return len(old)


def start_dispatcher():
    """平台后台只投递事件、处理过期租约，不运行 Agent 或测试。关闭时可中断 Redis 等待。"""
    stopped = threading.Event()
    migrate_sql_jobs()

    def loop():
        maintenance = 0
        while not stopped.is_set():
            try:
                while flush_outbox() == 100:
                    if stopped.is_set(): return
                from .executors import recover
                recover()
                if time.monotonic() >= maintenance:
                    reconcile()
                    from .storage import Session, DispatchEvent, now
                    from datetime import timedelta
                    with Session.begin() as db:
                        db.execute(delete(DispatchEvent).where(DispatchEvent.delivered_at < now()-timedelta(days=1)))
                    maintenance = time.monotonic()+300
            except Exception:
                # 不打印 Redis URL、数据库连接或 claim；重试持久化事件，后台异常不使业务假完成。
                log.warning("调度维护暂不可用，稍后重试")
            _wake.wait(10); _wake.clear()

    thread = threading.Thread(target=loop, name="redis-dispatch", daemon=True)
    thread.start()

    def close():
        stopped.set(); _wake.set(); thread.join(timeout=5)
    return close


def reconcile():
    """低频核对业务排队状态，补投 Redis 持久化故障后缺失的消息；在途任务不盲重跑。"""
    from .storage import Session, DispatchEvent, Record
    with Session.begin() as db:
        rows=db.scalars(select(Record).where(Record.kind.in_(["task","run","mcp_probe","local_action"]),Record.status.in_(["queued","running"])).with_for_update(skip_locked=True)).all()
        for row in rows:
            identity=row.payload.get("dispatchId")
            job=RedisJobs().get(identity)
            if row.status=="running":
                if not job or job.status not in {"running"}:
                    row.status="interrupted";row.payload=dict(row.payload,status="interrupted",summary="执行队列状态丢失，请确认实际结果后继续")
                continue
            if job and job.status in {"queued","running"}:continue
            if job:
                row.status="interrupted";row.payload=dict(row.payload,status="interrupted",summary="执行领取中断，请确认实际结果后继续")
                continue
            event=db.get(DispatchEvent,identity) if identity else None
            if event:
                # 已标记投递成功也可以补投；Redis 以 Job ID 去重。
                event.delivered_at=None
            else:enqueue(db,row,"agent" if row.kind=="task" else "test" if row.kind=="run" else "local")
    _wake.set()
