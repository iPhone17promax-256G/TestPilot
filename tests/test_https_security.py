"""Verify HTTPS rejection before credentials are parsed, without creating live test jobs."""
import asyncio

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from testpilot.api import app
from testpilot import api
from testpilot.config import settings
from testpilot.transport_security import RequireHTTPSMiddleware


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/auth/me", "/api/tools", "/mcp/"])
def test_platform_rejects_every_plain_http_path(path):
    response = TestClient(app, base_url="http://testserver").get(path)
    assert response.status_code == 426
    assert "HTTPS" in response.json()["message"]
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/setup", "/api/session"])
def test_plain_http_auth_rejected_before_validation_or_authentication(path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Plain HTTP must not reach authentication")

    monkeypatch.setattr(api.accounts, "login_account", unexpected)
    monkeypatch.setattr(api.accounts, "setup_account", unexpected)
    response = TestClient(app, base_url="http://testserver").post(
        path, content=b"this is deliberately not a JSON credential body",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 426
    assert "JSON" not in response.json()["message"]


def test_plain_http_cannot_spoof_tls_with_forwarded_headers():
    response = TestClient(app, base_url="http://testserver").post(
        "/api/auth/login", json={"username": "test-user", "password": "not-a-real-password"},
        headers={"X-Forwarded-Proto": "https", "Forwarded": "proto=https;host=localhost"},
    )
    assert response.status_code == 426
    assert "not-a-real-password" not in response.text


def test_guard_does_not_read_plaintext_request_body():
    sent = []

    async def forbidden_app(scope, receive, send):
        raise AssertionError("The downstream application must not run")

    async def forbidden_receive():
        raise AssertionError("The plaintext body must not be read")

    async def collect_send(message):
        sent.append(message)

    asyncio.run(RequireHTTPSMiddleware(forbidden_app)(
        {"type": "http", "scheme": "http", "method": "POST", "path": "/api/auth/login", "headers": []},
        forbidden_receive, collect_send,
    ))
    assert sent[0]["status"] == 426


def test_https_login_cookie_is_secure_and_can_be_reused(monkeypatch):
    user = {"id": "HTTPS-TEST-USER", "username": "https-test-user", "role": "member"}
    token = "https-unit-test-session-not-a-real-credential"
    observed = []
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(api.accounts, "login_account", lambda username, password: (token, user))
    monkeypatch.setattr(api.accounts, "has_users", lambda: True)

    def session_user(value):
        observed.append(value)
        return user if value == token else None

    monkeypatch.setattr(api.accounts, "session_user", session_user)
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/api/auth/login", json={"username": user["username"], "password": "not-a-real-password"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"] == user
    assert observed[-1] == token
    assert client.get("http://testserver/api/auth/me").status_code == 426


def test_https_compatibility_session_cookie_is_secure(monkeypatch):
    monkeypatch.setattr(settings, "access_token", "https-test-installation-token")
    response = TestClient(app, base_url="https://testserver").post(
        "/api/session", json={"token": "https-test-installation-token"},
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]


def test_https_logout_cookie_keeps_secure_attributes(monkeypatch):
    monkeypatch.setattr(settings, "access_token", "https-test-logout-token")
    monkeypatch.setattr(api.accounts, "session_user", lambda token: None)
    monkeypatch.setattr(api, "read_record", lambda *args: {"id": "PROJECT"})
    monkeypatch.setattr(api.accounts, "logout_account", lambda token: None)
    response = TestClient(app, base_url="https://testserver").post(
        "/api/auth/logout", headers={"Authorization": "Bearer https-test-logout-token"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=0" in cookie


def test_https_downstream_errors_also_receive_hsts():
    guarded = FastAPI()

    @guarded.get("/denied")
    def denied():
        from fastapi.responses import JSONResponse
        return JSONResponse({"message": "denied"}, status_code=403)

    guarded.add_middleware(RequireHTTPSMiddleware)
    response = TestClient(guarded, base_url="https://testserver").get("/denied")
    assert response.status_code == 403
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_websocket_requires_wss():
    guarded = FastAPI()

    @guarded.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("encrypted")
        await websocket.close()

    guarded.add_middleware(RequireHTTPSMiddleware)
    client = TestClient(guarded, base_url="https://testserver")
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("ws://testserver/ws"):
            pass
    assert error.value.code == 1008
    with client.websocket_connect("wss://testserver/ws") as connection:
        assert connection.receive_text() == "encrypted"
