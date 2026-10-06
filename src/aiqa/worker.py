# 历史直接访问数据库的 Worker 实现，保留兼容用途；不是当前 Compose 的正常启动链路。
# 学习当前架构请读 local_executor.py → executors.py → replica.py，平台不再运行此处的 Agent 循环。

import logging
import sys
import threading
import time
from datetime import timedelta
from sqlalchemy import select
from .workspace import project_scope
from .storage import Job, Record, initialize, now, session, uid, update_record

log = logging.getLogger("aiqa.worker")


def claim_job(kind):
    with session() as db:
        expired = list(db.scalars(select(Job).where(Job.kind == kind, Job.status == "running", Job.lease < now()).with_for_update(skip_locked=True)))
        for job in expired:
            job.status = "interrupted"
            row = db.get(Record, job.record_id)
            if row and row.status == "running":
                row.status = "interrupted"
                row.payload = dict(row.payload, status="interrupted", summary="Worker 租约过期，任务被标记为中断")
        job = db.scalar(select(Job).where(Job.kind == kind, Job.status == "queued").order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        row = db.get(Record, job.record_id)
        if row.status == "cancelled":
            job.status = "cancelled"
            return None
        token = uid("CLAIM")
        job.status, job.claim, job.lease = "running", token, now()+timedelta(seconds=45)
        row.status = "running"
        row.payload = dict(row.payload, status="running", summary="正在执行")
        return job.id, job.record_id, token


def heartbeat(job_id, token, stop):
    while not stop.wait(8):
        with session() as db:
            row = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not row or row.claim != token or row.status != "running":
                return
            row.lease = now()+timedelta(seconds=45)


# 旧 Worker 入口；当前启动脚本使用独立本地执行器，请勿为了“启动 Agent”在平台启用旧进程。
def main(kind):
    from .config import settings
    if settings.role != "legacy":raise RuntimeError("共享数据库 Worker 已停用；请启动 python -m aiqa.local_executor")
    initialize()
    logging.getLogger("browser_use").setLevel(logging.WARNING)
    cleanup_at = 0
    while True:
        claimed = claim_job(kind)
        if kind == "test" and time.monotonic() > cleanup_at:
            from .mobile import cleanup_expired
            cleanup_expired()
            from .mcp_client import cleanup_mcp_containers
            cleanup_mcp_containers()
            from .runners.container import cleanup_execution_containers
            cleanup_execution_containers()
            cleanup_at = time.monotonic()+10
        if not claimed:
            time.sleep(1)
            continue
        job_id, record_id, token = claimed
        log.info("Claimed %s %s (job %s)", kind, record_id, job_id)
        stop = threading.Event()
        thread = threading.Thread(target=heartbeat, args=(job_id, token, stop), daemon=True)
        thread.start()
        try:
            from .storage import read_record
            with project_scope(read_record(record_id).get("projectId", "PROJECT")):
                if kind == "test":
                    from .runners.container import execute_run
                    execute_run(record_id)
                else:
                    if record_id.startswith("PROBE-"):
                        from .extensions import probe_execute
                        probe_execute(record_id)
                    else:
                        from .agent.runtime import execute_task
                        execute_task(record_id)
        except Exception:
            log.exception("Worker execution failed for %s", record_id)
            update_record(record_id, lambda p: p.update(status="error", summary="执行进程发生错误，请查看服务端日志") if p["status"] not in {"cancelled", "interrupted"} else None)
        finally:
            if kind == "agent":
                from .mobile import close_task_sessions
                try:
                    from .storage import read_record
                    with project_scope(read_record(record_id).get("projectId","PROJECT")):close_task_sessions(record_id)
                except Exception: log.exception("Could not close mobile sessions for %s", record_id)
            from .storage import read_record
            log.info("Finished %s %s: %s", kind, record_id, read_record(record_id)["status"])
            stop.set()
            thread.join(timeout=2)
            with session() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job.claim == token and job.status == "running":
                    record=db.get(Record,record_id)
                    pending=kind=="agent" and record.status not in {"cancelled","waiting_approval"} and (record.payload.get("resumeRequested") or any(m["role"]=="user" and not m.get("delivered") for m in record.payload.get("messages",[])))
                    job.status = "queued" if pending else "finished"
                    job.lease = None
                    if pending:
                        record.status="queued"
                        record.payload=dict(record.payload,status="queued",resumeRequested=False,summary="继续处理新消息或审批结果")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main(sys.argv[1])
