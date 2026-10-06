import os
import uuid
import httpx


def test_nonexistent_account_is_rejected():
    response = httpx.post(os.environ["BASE_URL"] + "/api/v4/users/login", json={"login_id": "aiqa-missing-" + uuid.uuid4().hex, "password": "invalid-test-password"}, timeout=10)
    assert response.status_code == 401
    assert response.json()["status_code"] == 401
    assert response.json()["message"]
    assert not response.headers.get("Token")
