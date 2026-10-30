"""Fixtures for pre-existing tests which need a genuinely leased Redis Job."""
from testpilot.job_queue import RedisJobs


def mark_running(identity):
    queue=RedisJobs()
    job=queue.for_record(identity)
    try:queue.client.xgroup_create(job.stream,"workers",id="0")
    except Exception as error:
        if "BUSYGROUP" not in str(error):raise
    result=queue._take([(job.stream,[(job.message_id,{"jobId":job.id})])],job.account_id,job.executor_id or "TEST-EXECUTOR")
    assert result is not None
    return result
