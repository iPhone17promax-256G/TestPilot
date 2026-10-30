# 学习导航：Test: Step13 重点看 pytest_sessionfinish；pytest_runtest_makereport 在此前累积结果。
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


# 功能：pytest 会话启动时，根据容器环境配置建立网络证据记录器。
# 输入：session 为 pytest 传入的会话对象；TESTPILOT_NETWORK_CAPTURE 含会话 ID、目标 URL、采集级别。
# 输出/影响：返回 None；创建模块级 network_recorder，证据目录为 /work/network；未配置时不采集。
def pytest_sessionstart(session):
    global network_recorder
    configuration = os.environ.get("TESTPILOT_NETWORK_CAPTURE")
    if configuration:
        from testpilot.network_capture import NetworkRecorder
        config = json.loads(configuration)
        network_recorder = NetworkRecorder("/work/network", config["id"], config["targetUrl"], config["level"])


# fixture 将容器环境中的 BASE_URL 注入测试函数参数，避免每份用例写死环境地址。
# 功能：给生成测试提供当前运行环境地址，pytest 按参数名注入。
# 输入：无显式参数；BASE_URL 由容器外 Runner 冻结当前环境后注入进程。
# 输出/影响：返回 URL 字符串，不发请求、不保存数据；缺少环境变量会报错。
@pytest.fixture(scope="session")
def base_url():
    return os.environ["BASE_URL"]


# 功能：为 pytest-playwright 设置容器内 Chromium 的统一启动参数。
# 输入：无显式参数；pytest 按 fixture 名称自动读取返回配置。
# 输出/影响：返回无头模式、固定 Chromium 路径和容器兼容参数字典；由插件实际启动浏览器。
@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"headless": True, "executable_path": "/usr/lib/chromium/chromium", "args": ["--no-sandbox", "--disable-dev-shm-usage"]}


# pytest 将测试分为 setup、call、teardown；这个 hook 接收各阶段结果。
# 实际断言通常在 call，准备/清理也可能失败，所以报告不能只统计测试函数的返回值。
# 辅助钩子：pytest传item（某个测试项）与call（此次setup/call/teardown阶段的执行对象，当前函数未直接使用）。
# 产出：yield后取真实阶段报告，加入模块items列表；无业务返回值，sessionfinish随后写整体results.json。
# 数据：不是模型的“通过”描述；setup/teardown出错也留下证据，容器外classify据此区分环境错误。
# 功能：pytest 的包装钩子，收集实际测试阶段结果并挂到测试项供证据 fixture 使用。
# 输入：item 为当前测试项；call 为当前 setup/call/teardown 执行对象；yield 后取得其他钩子生成的报告。
# 输出/影响：不返回业务值；内存 items 保存主体结果及准备/清理失败，最终由 sessionfinish 写 results.json。
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # 等待 pytest 其他钩子生成当前阶段的真实报告，再挂到测试项供证据 fixture 收尾读取。
    # 测试主体结果连同准备/清理的失败或跳过一起保存在模块 items 内存，会话结束时才统一写 results.json。
    result = yield
    report = result.get_result()
    setattr(item, "rep_" + report.when, report)
    if report.when == "call" or report.failed or report.skipped:
        items.append({"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome, "duration": report.duration, "error": str(report.longrepr)[:18000] if report.failed or report.skipped else ""})


# 收集阶段还没有执行测试；导入错误、语法问题等也必须写入报告，避免误报“没有失败所以通过”。
# 功能：收集导入或发现测试时的错误，避免零用例被误判为通过。
# 输入：report 是 pytest 收集阶段报告，含 failed 和 longrepr 错误详情。
# 输出/影响：返回 None；失败文本限长追加到 collection_errors，随后统一写整体报告。
def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(str(report.longrepr)[:18000])


# 退出时落盘 results.json 和网络证据，供容器外 execute_run 读取。
# 采集收尾出错要记录原因，但不能伪造测试 verdict。
# Test: Step13 [开始] pytest 会调用这个钩子，将退出码、收集数量、测试项与收集错误写入 results.json。
# 职责：pytest 整个测试会话结束时汇总报告，供容器外 Runner 判断最终结果。
# 输入：pytest 自动传 session（会话对象，含 testscollected）和 exitstatus（进程测试退出状态）。
# 输入来源：模块 items 来自前面的 makereport 钩子，collection_errors 来自收集钩子，不是模型生成的结论。
# 返回：无业务返回值（None）；真正输出是 /work/results.json，含退出码、收集数、各阶段结果与收集错误。
# 存储：/work 挂在本次 runs/运行ID/ 目录；results.json 是整体证据，steps.json 是动作级证据，两者互补。
# 边界：在测试容器内写文件，不直连平台数据库；网络采集收尾失败单独记日志，不伪造测试判定。
# pytest 结束时传入会话对象和退出码；测试数量从 session.testscollected 读取，平台不手工调用此钩子。
# 概念：pytest 是发现并执行 test_ 函数的测试框架；fixture 提供准备资源，hook 是框架在收集/执行/收尾阶段主动调用的扩展函数。
# 这里 session 是 pytest 测试会话对象，不是数据库 Session、Agent Task 或浏览器 Context。平台无需自己手动调用这个 hook。
# 收集先导入模块并找到测试项；每项再经历 setup（准备）、call（测试主体）、teardown（清理），前面的报告 hook 已累积这些真实结果。
# steps.json 是 TestPilot 可视化动作级证据，results.json 是 pytest 测试项/阶段级证据；一个 pytest 测试函数中可以执行很多可视化步骤。
# exitstatus 是 pytest 退出结果，collected 是找到的测试数量；没有报错但零用例/全跳过也不能证明测试通过，下一步 Runner 会结合两者判定。
def pytest_sessionfinish(session, exitstatus):
    # 先收尾附加的网络证据，移除监听并保存索引；即使取证失败也只记有限日志，不能改变已经得到的测试判定。
    # 随后把实际退出码、收集数量、items 和导入错误写到 /work/results.json，容器外 classify 据此判定整次 Run。
    if network_recorder:
        try:
            network_recorder.close()
        except Exception as error:
            print("网络记录未正常结束：", str(error)[:200])
    Path("/work/results.json").write_text(json.dumps({"exitCode": int(exitstatus), "collected": session.testscollected, "items": items, "collectionErrors": collection_errors}, ensure_ascii=False))
    # Test: Step13 [结束] 下一步：Test: Step14 → src/testpilot/runners/container.py 的 refresh_at/监控循环。
    # 这是从测试容器回到容器外 Runner；Runner 读取刚生成的 results.json，不靠模型口头判断通过。


# 自动给使用 page fixture 的测试开启截图、trace、控制台和网络证据采集。
# yield 前准备，yield 后收尾；Playwright 操作与 expect 有等待上限，不靠固定 sleep 猜页面加载时间。
# 功能：自动给使用 page 的测试记录截图、trace、控制台错误和可选网络证据。
# 输入：request 为 pytest fixture 请求，可取 page 和测试 nodeid；network_recorder 是会话启动时的可选采集器。
# 输出/影响：yield 将控制权交给测试；设置等待上限，收尾写 /work/哈希名称证据文件，采集错误另写文件。
@pytest.fixture(autouse=True)
def record_browser_evidence(request):
    # 准备阶段：只有使用 page 的测试需要浏览器证据；取得它自己的上下文，绑定可选网络记录并设置有界动作/导航/断言等待。
    # 测试名称生成稳定文件前缀，控制台错误先留内存，trace 从此处开始；yield 后测试动作才执行。
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
    # 测试返回后的取证阶段：保存最后页面截图、完整 trace 和已收集的控制台文本到本次 /work。
    # 取证异常另写 capture-error 文件供排查，不用截图是否成功来冒充业务断言结论。
    try:
        page.screenshot(path=f"/work/{key}.png", full_page=True)
        page.context.tracing.stop(path=f"/work/{key}-trace.zip")
        Path(f"/work/{key}-console.txt").write_text("\n".join(console))
    except Exception as error:
        Path(f"/work/{key}-capture-error.txt").write_text(str(error))


# 功能：APP 测试收集后检查 driver 是否来自平台 fixture，以便管理设备会话与取消。
# 输入：items 为 pytest 收集到的测试项列表；TESTPILOT_APP_CAPABILITIES 存在表示本次是平台 APP 运行。
# 输出/影响：返回 None；普通测试不检查，APP 用错 driver fixture 抛 UsageError，不执行测试动作。
def pytest_collection_modifyitems(items):
    if not os.environ.get("TESTPILOT_APP_CAPABILITIES"):
        return
    for item in items:
        definitions=item._fixtureinfo.name2fixturedefs.get("driver",())
        if "driver" not in item.fixturenames or not definitions or definitions[-1].func.__module__!="testpilot.runners.app_plugin":
            raise pytest.UsageError("APP 测试必须使用平台 driver fixture，以便管理设备、保存证据和取消会话")
