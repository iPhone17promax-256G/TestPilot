"""Queue tests use real Redis, with an isolated prefix and no live executor consumers."""
import secrets
import pytest
from testpilot.config import settings
from testpilot.job_queue import RedisJobs


@pytest.fixture(scope="session",autouse=True)
def isolated_queue():
    original=settings.redis_prefix
    settings.redis_prefix="testpilot:checks:"+secrets.token_hex(12)
    queue=RedisJobs()
    queue.client.ping()
    yield queue
    # Only delete this test invocation's keys. Never FLUSHDB a Redis instance shared with the application.
    keys=list(queue.client.scan_iter(match=settings.redis_prefix+":*"))
    if keys:queue.client.delete(*keys)
    settings.redis_prefix=original
