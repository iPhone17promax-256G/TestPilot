"""Mobile readiness and startup regressions without a real device or queue."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from testpilot import executors, mobile, storage


@pytest.mark.parametrize("node_url,ready,devices,expected", [
    ("", True, [], "unknown"),
    ("", False, [], "offline"),
    ("http://node", True, [dict(udid="phone", state="device", androidVersion="11")], "online"),
    ("http://node", True, [dict(udid="another-phone", state="device", androidVersion="11")], "offline"),
    ("http://node", True, [dict(udid="phone", state="unauthorized", androidVersion="")], "offline"),
    ("http://node", False, [dict(udid="phone", state="device", androidVersion="11")], "offline"),
])
def test_device_readiness_requires_matching_online_udid(monkeypatch, node_url, ready, devices, expected):
    device = dict(id="DEVICE-DEMO", name="Phone", udid="phone",
                  appiumUrl="http://appium", nodeUrl=node_url)
    requests = []

    def get(url, **kwargs):
        requests.append(url)
        payload = dict(value=dict(ready=ready)) if url.endswith("/status") else dict(devices=devices)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(mobile, "read_record", lambda *args: dict(device))
    monkeypatch.setattr(mobile.httpx, "get", get)
    monkeypatch.setattr(mobile, "update_record", lambda identity, change: change(device))
    monkeypatch.setattr(mobile, "devices_list", lambda: [device])

    result = mobile.device_check(device["id"])

    assert result["status"] == expected
    if expected == "unknown":
        assert "Appium 可达" in result["message"] and "设备待验证" in result["message"]
        assert requests == ["http://appium/status"]
    if node_url:
        assert requests == ["http://appium/status", "http://node/health"]


def test_unreachable_device_node_is_offline(monkeypatch):
    device = dict(id="DEVICE-DEMO", udid="phone", appiumUrl="http://appium", nodeUrl="http://node")

    def get(url, **kwargs):
        if url.endswith("/health"):
            raise httpx.ConnectError("node unavailable")
        return httpx.Response(200, json=dict(value=dict(ready=True)), request=httpx.Request("GET", url))

    monkeypatch.setattr(mobile, "read_record", lambda *args: dict(device))
    monkeypatch.setattr(mobile.httpx, "get", get)
    monkeypatch.setattr(mobile, "update_record", lambda identity, change: change(device))
    monkeypatch.setattr(mobile, "devices_list", lambda: [device])

    assert mobile.device_check(device["id"])["status"] == "offline"


@pytest.mark.parametrize("lease_lost", [False, True])
def test_open_renews_after_apk_preparation_before_creating_native_session(monkeypatch, lease_lost):
    # Preparing an APK consumes most of the first lease. Native startup must get
    # a fresh lease, and must not begin at all when ownership has been lost.
    clock = dict(seconds=0, expires=90)
    device = dict(id="DEVICE-DEMO", name="Phone", udid="phone", appiumUrl="http://appium")
    app = dict(id="APP-DEMO", name="Demo", path="apps/demo.apk")
    events, saved = [], []

    def prepare(*args):
        clock["seconds"] = 85
        events.append("apk-ready")
        return dict(platformName="Android")

    def renew(device_id, owner, native=""):
        events.append(("renew", native))
        if lease_lost:
            raise RuntimeError("device lease lost")
        clock["expires"] = clock["seconds"] + 90

    def webdriver(actual_device, method, path, body):
        assert (method, path) == ("POST", "/session")
        assert body["capabilities"]["alwaysMatch"] == dict(platformName="Android")
        assert clock["expires"] - clock["seconds"] == 90
        events.append("native-open")
        clock["seconds"] += 65
        return dict(sessionId="native-demo")

    @contextmanager
    def session():
        yield SimpleNamespace(add=lambda record: saved.append(record.payload))

    monkeypatch.setattr(mobile, "read_record", lambda identity, kind: dict(device if kind == "device" else app))
    monkeypatch.setattr(mobile, "acquire", lambda *args: dict(device))
    monkeypatch.setattr(mobile, "capabilities", prepare)
    monkeypatch.setattr(mobile, "renew", renew)
    monkeypatch.setattr(mobile, "webdriver", webdriver)
    monkeypatch.setattr(mobile, "session", session)
    monkeypatch.setattr(mobile, "release", lambda *args: events.append("released"))

    if lease_lost:
        with pytest.raises(RuntimeError, match="device lease lost"):
            mobile.mobile_action("open", device_id=device["id"], app_id=app["id"])
        assert events == ["apk-ready", ("renew", ""), "released"]
        assert saved == []
    else:
        result = mobile.mobile_action("open", device_id=device["id"], app_id=app["id"])
        assert events == ["apk-ready", ("renew", ""), "native-open", ("renew", "native-demo")]
        assert saved[0]["nativeId"] == "native-demo"
        assert result["session_id"] == saved[0]["id"]


@pytest.mark.parametrize("name,arguments,completion_at,expected_timeout", [
    ("mobile_action", dict(action="open"), 75, None),
    ("mobile_action", dict(action="open"), 130, 120),
    ("mobile_action", dict(action="observe"), 75, 60),
    ("devices_check", dict(id="DEVICE-DEMO"), 75, 60),
])
def test_only_mobile_open_waits_for_slow_startup(monkeypatch, name, arguments, completion_at, expected_timeout):
    import time

    clock = dict(seconds=0)
    saved = []

    @contextmanager
    def session():
        yield SimpleNamespace(add=lambda record: saved.append(record.payload))

    def read_record(*args):
        if clock["seconds"] >= completion_at:
            return dict(status="completed", result=dict(session_id="MOBILE-DEMO"))
        return dict(status="queued")

    monkeypatch.setattr(time, "monotonic", lambda: clock["seconds"])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.update(seconds=clock["seconds"] + seconds))
    monkeypatch.setattr(executors, "session", session)
    monkeypatch.setattr(executors, "selected_executor", lambda: "EXECUTOR-DEMO")
    monkeypatch.setattr(executors, "enqueue", lambda *args: None)
    monkeypatch.setattr(executors, "read_record", read_record)
    monkeypatch.setattr(storage, "update_record", lambda identity, change: change(saved[0]))

    if expected_timeout is None:
        assert executors.local_tool(name, arguments) == dict(session_id="MOBILE-DEMO")
        assert clock["seconds"] == 75
        assert saved[0]["status"] == "queued"
    else:
        with pytest.raises(ValueError, match="本地执行器未及时响应"):
            executors.local_tool(name, arguments)
        assert clock["seconds"] == expected_timeout
        assert saved[0]["status"] == "cancelled"
