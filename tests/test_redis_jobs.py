"""Actual Redis Streams and SQL transactions, including delivery/lease failure paths."""
import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from sqlalchemy import select, inspect
from testpilot import executors
from testpilot.config import settings
from testpilot.job_queue import RedisJobs, enqueue, flush_outbox, complete, reconcile, wait_available
from testpilot.storage import Record, DispatchEvent, session, uid, read_record, engine
from testpilot.workspace import user_scope, project_scope


def job_data(account=None,executor="queue-executor",kind="test"):
    return dict(id=uid("JOB"),kind=kind,record_id=uid("RUN"),account_id=account or uid("USER"),project_id="PROJECT",executor_id=executor)


def test_repeated_delivery_creates_one_stream_message(isolated_queue):
    data=job_data();event=dict(op="enqueue",job=data)
    for _ in range(3):isolated_queue.publish(event)
    assert isolated_queue.client.xlen(isolated_queue.stream(data["account_id"],data["executor_id"],data["kind"]))==1


def test_concurrent_claim_has_one_owner_and_one_execution(isolated_queue):
    data=job_data();isolated_queue.publish(dict(op="enqueue",job=data))
    barrier=threading.Barrier(8)
    def take():
        barrier.wait();return isolated_queue.claim(data["account_id"],data["executor_id"],data["kind"])
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:take(),range(8)))
    claimed=[j for j in results if j]
    assert len(claimed)==1 and claimed[0].id==data["id"]
    assert isolated_queue.claim(data["account_id"],data["executor_id"],data["kind"]) is None


def test_blocked_reader_wakes_when_job_arrives(isolated_queue):
    data=job_data(account=uid("USER"));started=threading.Event()
    def take():
        started.set();return isolated_queue.claim(data["account_id"],data["executor_id"],data["kind"],2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(take);assert started.wait(1)
        isolated_queue.publish(dict(op="enqueue",job=data))
        assert future.result(timeout=3).id==data["id"]


def test_wait_notification_does_not_claim_or_start_lease(isolated_queue):
    data=job_data(account=uid("USER"));isolated_queue.publish(dict(op="enqueue",job=data))
    assert asyncio.run(wait_available(data["account_id"],data["executor_id"],"test",1))
    job=isolated_queue.get(data["id"])
    assert job.status=="queued" and not job.claim and job.lease is None


def test_accounts_and_target_executors_are_isolated(isolated_queue):
    data=job_data(account=uid("USER"));isolated_queue.publish(dict(op="enqueue",job=data))
    assert isolated_queue.claim("another-account",data["executor_id"],"test") is None
    assert isolated_queue.claim(data["account_id"],"another-executor","test") is None
    assert isolated_queue.claim(data["account_id"],data["executor_id"],"test").id==data["id"]


def test_disconnected_reader_does_not_lose_unstarted_shared_job(isolated_queue):
    data=job_data(account=uid("USER"),executor="")
    isolated_queue.publish(dict(op="enqueue",job=data))
    stream=isolated_queue.stream(data["account_id"],"","test")
    isolated_queue.client.xgroup_create(stream,"workers",id="0")
    isolated_queue.client.xreadgroup("workers","disconnected-api",{stream:">"},count=1)
    job=isolated_queue.claim(data["account_id"],"replacement","test")
    assert job and job.id==data["id"]


def test_terminal_ack_removes_delivery_and_old_token_cannot_renew(isolated_queue):
    data=job_data(account=uid("USER"));isolated_queue.publish(dict(op="enqueue",job=data))
    job=isolated_queue.claim(data["account_id"],data["executor_id"],"test")
    isolated_queue.renew(job)
    isolated_queue.publish(dict(op="finish",job=data,claim=job.claim,status="finished"))
    assert isolated_queue.get(job.id).status=="finished"
    assert isolated_queue.client.xlen(job.stream)==0
    assert isolated_queue.client.xpending(job.stream,"workers")["pending"]==0
    with pytest.raises(PermissionError):isolated_queue.renew(job)


def new_run(account,executor=""):
    identity=uid("RUN")
    with user_scope(account),session() as db:
        row=Record(id=identity,kind="run",status="queued",payload=dict(id=identity,status="queued",ownerId=account,executorId=executor))
        db.add(row);enqueue(db,row,"test")
    return identity


def test_sql_rollback_never_dispatches(isolated_queue):
    identity=uid("RUN")
    with pytest.raises(RuntimeError),session() as db:
        row=Record(id=identity,kind="run",status="queued",payload=dict(id=identity,status="queued"))
        db.add(row);enqueue(db,row,"test")
        raise RuntimeError("business rollback")
    assert isolated_queue.for_record(identity) is None
    with session() as db:assert db.scalar(select(DispatchEvent).where(DispatchEvent.record_id==identity)) is None


def test_unavailable_redis_keeps_business_and_retries_outbox(monkeypatch,isolated_queue):
    owner=uid("USER")
    with monkeypatch.context() as fail:
        fail.setattr(RedisJobs,"publish",lambda *args:(_ for _ in ()).throw(redis.ConnectionError("queue unavailable")))
        identity=new_run(owner)
    with user_scope(owner):assert read_record(identity)["status"]=="queued"
    assert isolated_queue.for_record(identity) is None
    flush_outbox()
    assert isolated_queue.for_record(identity).status=="queued"


def test_claim_authentication_and_lease_expiry_do_not_reexecute(isolated_queue):
    owner=uid("USER")
    with user_scope(owner):
        executor=executors.register("","queue audit",{})
        identity=new_run(owner,executor["id"])
        leased=executors.claim(executor["id"],"test")
        job=isolated_queue.for_record(identity)
        with project_scope(leased["projectId"]):
            assert executors.heartbeat(leased["claim"])["status"]=="running"
            with user_scope(uid("USER")),pytest.raises(PermissionError):executors.heartbeat(leased["claim"])
            isolated_queue.client.hset(isolated_queue.key("job",job.id),"lease",time.time()-1)
            isolated_queue.client.zadd(isolated_queue.key("leases"),{job.id:time.time()-1})
            with pytest.raises(PermissionError):executors.heartbeat(leased["claim"])
        executors.recover()
        assert read_record(identity)["status"]=="interrupted"
        assert isolated_queue.for_record(identity).status=="interrupted"
        assert executors.claim(executor["id"],"test") is None


def test_queued_job_missing_from_redis_is_rebuilt_from_outbox(isolated_queue):
    owner=uid("USER");identity=new_run(owner)
    job=isolated_queue.for_record(identity)
    isolated_queue.client.delete(isolated_queue.key("job",job.id),isolated_queue.key("record",identity),job.stream)
    reconcile();flush_outbox()
    restored=isolated_queue.for_record(identity)
    assert restored.id==job.id and restored.status=="queued"


def test_completed_result_is_not_reexecuted_after_stale_queue_delivery(isolated_queue):
    owner=uid("USER")
    with user_scope(owner):
        executor=executors.register("","queue audit",{})
        identity=new_run(owner,executor["id"])
        with session() as db:
            row=db.get(Record,identity);row.status="passed";row.payload=dict(row.payload,status="passed")
        assert executors.claim(executor["id"],"test") is None
        assert read_record(identity)["status"]=="passed"


def test_agent_finish_schedules_new_generation_and_rejects_old_claim(isolated_queue):
    owner=uid("USER");identity=uid("TASK")
    with user_scope(owner):
        executor=executors.register("","agent audit",{})
        with session() as db:
            row=Record(id=identity,kind="task",status="queued",payload=dict(id=identity,status="queued",ownerId=owner,executorId=executor["id"],messages=[]))
            db.add(row);enqueue(db,row,"agent")
        first=executors.claim(executor["id"],"agent")
        with project_scope(first["projectId"]):
            with session() as db:
                row=db.get(Record,identity);row.status="completed"
                row.payload=dict(row.payload,status="completed",messages=[dict(id="new",role="user",delivered=False,content="followup")])
            executors.finish(first["claim"])
            second=isolated_queue.for_record(identity)
            assert second.id!=first["jobId"] and second.status=="queued"
            with pytest.raises(PermissionError):executors.heartbeat(first["claim"])
            assert executors.claim(executor["id"],"agent")["jobId"]==second.id


def test_schema_has_outbox_and_no_sql_job_table():
    names=inspect(engine).get_table_names()
    assert "dispatch_outbox" in names and "jobs" not in names


def test_recovery_rechecks_lease_after_heartbeat(monkeypatch,isolated_queue):
    owner=uid("USER")
    with user_scope(owner):
        executor=executors.register("","renew race",{})
        identity=new_run(owner,executor["id"])
        executors.claim(executor["id"],"test")
        active=isolated_queue.for_record(identity)
        stale=copy.deepcopy(active)
        from datetime import timedelta
        from testpilot.storage import now
        stale.lease=now()-timedelta(seconds=1)
        monkeypatch.setattr(RedisJobs,"expired",lambda self:[stale])
        executors.recover()
        assert read_record(identity)["status"]=="running"
        assert isolated_queue.get(active.id).claim==active.claim


def test_agent_created_run_is_dispatched_on_platform_commit(isolated_queue):
    owner=uid("USER");task_id=uid("TASK");run_id=uid("RUN")
    with user_scope(owner):
        executor=executors.register("","commit audit",{})
        with session() as db:
            row=Record(id=task_id,kind="task",status="queued",payload=dict(id=task_id,status="queued",ownerId=owner,executorId=executor["id"],messages=[]))
            db.add(row);enqueue(db,row,"agent")
        leased=executors.claim(executor["id"],"agent")
        with project_scope(leased["projectId"]):
            assert executors.commit(leased["claim"],[dict(id=run_id,kind="run",base=None,payload=dict(id=run_id,status="queued",taskId=task_id))])["status"]=="ok"
            run=read_record(run_id)
            job=isolated_queue.for_record(run_id)
            assert job.kind=="test" and job.account_id==owner and job.executor_id==executor["id"]
            assert run["dispatchId"]==job.id
            assert executors.claim(executor["id"],"test")["recordId"]==run_id


def test_bulk_snapshot_reads_current_jobs(isolated_queue):
    owner=uid("USER");identities=[new_run(owner) for _ in range(3)]
    jobs=isolated_queue.for_records([uid("MISSING"),*identities])
    assert {j.record_id for j in jobs}==set(identities)
    assert all(j.account_id==owner and j.status=="queued" for j in jobs)


def test_legacy_jobs_migrate_then_old_table_is_removed(monkeypatch,tmp_path,isolated_queue):
    from sqlalchemy import create_engine,text
    from sqlalchemy.orm import sessionmaker
    from testpilot import storage
    from testpilot.job_queue import migrate_sql_jobs
    local_engine=create_engine("sqlite:///"+str(tmp_path/"old-platform.sqlite"))
    storage.Base.metadata.create_all(local_engine)
    local_session=sessionmaker(local_engine)
    ids=[uid("JOB") for _ in range(3)];records=[uid("RUN") for _ in ids]
    with local_session.begin() as db:
        for record_id,status in zip(records,["queued","running","passed"]):
            db.add(Record(id=record_id,kind="run",status=status,payload=dict(id=record_id,status=status,ownerId="migration-owner")))
    with local_engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT, record_id TEXT, status TEXT, claim TEXT)"))
        for identity,record_id,status in zip(ids,records,["queued","running","finished"]):
            connection.execute(text("INSERT INTO jobs VALUES (:id,'test',:record_id,:status,'old-secret')"),dict(id=identity,record_id=record_id,status=status))
    monkeypatch.setattr(storage,"engine",local_engine)
    monkeypatch.setattr(storage,"Session",local_session)
    monkeypatch.setattr(settings,"data",tmp_path)
    assert migrate_sql_jobs()==3
    assert "jobs" not in inspect(local_engine).get_table_names()
    assert (tmp_path/"migrations/jobs-before-redis.json").is_file()
    assert migrate_sql_jobs()==0
    with local_session() as db:
        assert db.get(Record,records[1]).status=="interrupted"
        assert db.get(Record,records[2]).status=="passed"
        events=db.scalars(select(DispatchEvent)).all()
        for event in events:isolated_queue.publish(event.payload)
    assert isolated_queue.get(ids[0]).status=="queued"
    assert isolated_queue.get(ids[1]).status=="interrupted"
    assert isolated_queue.get(ids[2]).status=="finished"
    assert all(not isolated_queue.get(identity).claim for identity in ids)
    local_engine.dispose()
