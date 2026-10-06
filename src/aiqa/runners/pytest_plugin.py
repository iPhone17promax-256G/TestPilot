# 在测试容器内部由 pytest 自动加载的钩子和 fixture。
# 这些函数名是 pytest 的扩展约定；平台不需要手写调用每个 hook。

"""Runs inside the unprivileged test container. No database or model credentials."""
import hashlib
import json
import os
from pathlib import Path
import pytest

items = []
collection_errors = []
network_recorder = None


def pytest_sessionstart(session):
    global network_recorder
    configuration = os.environ.get("AIQA_NETWORK_CAPTURE")
    if configuration:
        from aiqa.network_capture import NetworkRecorder
        config = json.loads(configuration)
        network_recorder = NetworkRecorder("/work/network", config["id"], config["targetUrl"], config["level"])


# fixture 将容器环境中的 BASE_URL 注入测试函数参数，避免每份用例写死环境地址。
@pytest.fixture(scope="session")
def base_url():
    return os.environ["BASE_URL"]


@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"headless": True, "executable_path": "/usr/lib/chromium/chromium", "args": ["--no-sandbox", "--disable-dev-shm-usage"]}


# pytest 将测试分为 setup、call、teardown；这个 hook 接收各阶段结果。
# 实际断言通常在 call，准备/清理也可能失败，所以报告不能只统计测试函数的返回值。
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    setattr(item, "rep_" + report.when, report)
    if report.when == "call" or report.failed or report.skipped:
        items.append({"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome, "duration": report.duration, "error": str(report.longrepr)[:18000] if report.failed or report.skipped else ""})


# 收集阶段还没有执行测试；导入错误、语法问题等也必须写入报告，避免误报“没有失败所以通过”。
def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(str(report.longrepr)[:18000])


# 退出时落盘 results.json 和网络证据，供容器外 execute_run 读取。
# 采集收尾出错要记录原因，但不能伪造测试 verdict。
def pytest_sessionfinish(session, exitstatus):
    if network_recorder:
        try:
            network_recorder.close()
        except Exception as error:
            # Evidence collection is optional; preserve the test verdict even
            # if the recorder or its output directory is unavailable.
            print("网络记录未正常结束：", str(error)[:200])
    Path("/work/results.json").write_text(json.dumps({"exitCode": int(exitstatus), "collected": session.testscollected, "items": items, "collectionErrors": collection_errors}, ensure_ascii=False))


# 自动给使用 page fixture 的测试开启截图、trace、控制台和网络证据采集。
# yield 前准备，yield 后收尾；Playwright 操作与 expect 有等待上限，不靠固定 sleep 猜页面加载时间。
@pytest.fixture(autouse=True)
def record_browser_evidence(request):
    if "page" not in request.fixturenames:
        yield
        return
    page = request.getfixturevalue("page")
    if network_recorder and not any(context is page.context for context, _, _ in network_recorder.contexts):
        network_recorder.bind_sync(page.context)
    page.set_default_timeout(15000)
    page.set_default_navigation_timeout(30000)
    from playwright.sync_api import expect
    expect.set_options(timeout=10000)
    key = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    console = []
    page.on("pageerror", lambda error: console.append(str(error)))
    page.on("console", lambda message: console.append(message.type + ": " + message.text) if message.type in {"warning", "error"} else None)
    page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
    yield
    try:
        page.screenshot(path=f"/work/{key}.png", full_page=True)
        page.context.tracing.stop(path=f"/work/{key}-trace.zip")
        Path(f"/work/{key}-console.txt").write_text("\n".join(console))
    except Exception as error:
        Path(f"/work/{key}-capture-error.txt").write_text(str(error))


def pytest_collection_modifyitems(items):
    if not os.environ.get("AIQA_APP_CAPABILITIES"):
        return
    for item in items:
        definitions=item._fixtureinfo.name2fixturedefs.get("driver",())
        if "driver" not in item.fixturenames or not definitions or definitions[-1].func.__module__!="aiqa.runners.app_plugin":
            raise pytest.UsageError("APP 测试必须使用平台 driver fixture，以便管理设备、保存证据和取消会话")
