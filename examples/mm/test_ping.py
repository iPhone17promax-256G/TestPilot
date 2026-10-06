import os
import httpx


def test_ping():
    response = httpx.get(os.environ["BASE_URL"] + "/api/v4/system/ping", timeout=10)
    assert response.status_code == 200
    assert response.json()["status"] == "OK"
