import pytest
from fastapi.testclient import TestClient
from aiqa.api import app
from aiqa.config import settings


@pytest.fixture(scope="module")
def client():
    # The MCP SDK manager owns one lifecycle per process, just like the API.
    with TestClient(app) as instance:
        yield instance


def test_shared_token_accepts_header_and_http_only_session(client, monkeypatch):
    monkeypatch.setattr(settings, "access_token", "local-acceptance-token")
    assert client.get("/api/tools").status_code == 401
    assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
    assert client.get("/api/tools", headers={"Authorization": "Bearer local-acceptance-token"}).status_code == 200
    response = client.post("/api/session", json={"token": "local-acceptance-token"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/tools").status_code == 200


@pytest.mark.parametrize("name", ["workspace_exec", "browser_explore"])
def test_worker_tools_are_not_executed_in_the_http_process(name, client, monkeypatch):
    client.headers["Authorization"]="Bearer "+settings.access_token
    names = [t["name"] for t in client.get("/api/tools").json()]
    assert name not in names
    response = client.post("/api/tools/"+name, json={"arguments": {}})
    assert response.status_code == 400
    assert "Agent Worker" in response.json()["detail"]
