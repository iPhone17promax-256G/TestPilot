# browser-use 的即时页面探索，供 Agent 看页面、寻找稳定定位方式和收集证据。
# 探索完成不等于回归用例已通过；可复跑的测试资产仍需另行保存并由 Runner 执行。

"""Live exploration is evidence; saved pytest code remains the regression asset."""
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from browser_use import Agent, Browser
from ..config import settings
from ..storage import read_record, uid, update_record
from ..testing import model_settings, safe_path, stamp, write_file
from .container import redact
from ..agent.browser_model import BrowserModel
from ..network_capture import NetworkRecorder
from ..network import create_session, resolve_level, session_summary


# 创建本次观察独占的 Chromium/browser-use Agent，目标限定为所选环境，结果关联当前 Task。
# 同步工具接口里用 asyncio.run 驱动内部异步流程；这里不是用户电脑上的 Chrome 扩展会话。
# 功能：为 Agent 启动一次真实页面探索，收集稳定定位建议、截图及独立网络证据。
# 输入：task_id 为当前 Agent 任务；environment_id 为所选环境；goal 为探索目标；path 为环境内起始路径。
# 输出/影响：返回观察结果字典；读当前模型设置，创建 inspections 目录和网络会话，用 asyncio.run 驱动内部浏览器流程。
def explore(task_id, environment_id, goal, path="/"):
    if not task_id:
        raise ValueError("页面探索需要 Agent task_id")
    env = read_record(environment_id, "environment")
    model = model_settings()
    identity = uid("INS")
    relative = f"inspections/{identity}"
    safe_path(relative).mkdir(parents=True, exist_ok=True)
    target = urljoin(env["executionUrl"]+"/", path)
    if urlparse(target).netloc != urlparse(env["executionUrl"]).netloc:
        raise ValueError("探索地址必须属于当前测试环境")
    browser_path = Path("/usr/lib/chromium/chromium")
    if not browser_path.is_file():
        raise RuntimeError("未安装 Chromium")
    capture = create_session(identity, "inspection", relative, env["executionUrl"], resolve_level(task_id), task_id)
    recorder = NetworkRecorder(safe_path(capture["path"]), capture["id"], env["executionUrl"], capture["level"])

    # browser-use 自身也有多步决策循环，属于外层 Agent 调用的一个探索工具。
    # 开启网络记录时，Playwright 通过 CDP 只监听刚创建的 Chromium，不接管其他浏览器流量。
    # 有步数/总时间上限；finally 收集截图、网络摘要并关闭本次浏览器。
    # 功能：执行 browser-use 多步探索并统一收尾本次独占浏览器与证据，作为同步工具内部的异步实现。
    # 输入：无显式参数；捕获外层 task_id、env、target、model、capture、recorder、identity 和结果目录。
    # 输出/影响：返回观察结果；调用模型和本次 Chromium，限 8 步/300 秒；保存 observation.json/截图并关联 Task，不创建回归 Run。
    async def run():
        browser = Browser(headless=True, executable_path=str(browser_path), args=["--no-sandbox", "--disable-dev-shm-usage"], enable_default_extensions=False, allowed_domains=[env["executionUrl"]], user_data_dir=None)
        llm = BrowserModel(model=model["model"], base_url=model["baseUrl"], api_key=settings.model_key, reasoning_effort=None, reasoning_models=[], temperature=None, frequency_penalty=None, max_completion_tokens=settings.model_max_tokens)
        # 功能：让 browser-use 每轮确认外层 Agent 任务是否已被用户停止。
        # 输入：无显式参数；task_id 来自 explore 外层闭包，状态从任务记录实时读取。
        # 输出/影响：异步返回是否 cancelled/interrupted 的布尔值；不改任务，供框架停止探索使用。
        async def should_stop():
            return read_record(task_id)["status"] in {"cancelled", "interrupted"}
        agent = Agent(
            task=f"测试地址：\n{target}\n\n{goal}\n只观察测试目标并进行必要的低影响操作，不创建或删除业务数据。最后调用 done，text 用中文报告真实页面、稳定的 role/label/CSS 定位建议和观察到的错误。页面元素序号不是永久定位器。",
            llm=llm, browser=browser,
            initial_actions=[{"navigate": {"url": target, "new_tab": False}}],
            directly_open_url=False, use_vision=True, use_thinking=False,
            use_judge=False, max_actions_per_step=3, max_failures=2,
            calculate_cost=False, register_should_stop_callback=should_stop,
        )
        result = {"id": identity, "createdAt": stamp(), "status": "error", "summary": "", "urls": [], "actions": [], "artifacts": []}
        history = None
        playwright = None
        try:
            if capture["level"] != "off":
                await asyncio.wait_for(browser.start(), timeout=45)
                try:
                    # 这个 Playwright 旁路连接只附着到本次 browser-use 启动的 Chromium，订阅网络事件。
                    # 旁路观察器自己不导航、不拦截请求、不关标签页；页面操作由 browser-use 的独立控制流程负责，两者分工不同。
                    from playwright.async_api import async_playwright
                    playwright = await async_playwright().start()
                    observed = await playwright.chromium.connect_over_cdp(browser.cdp_url, timeout=10000)
                    for context in observed.contexts:
                        recorder.bind_async(context)
                except Exception as error:
                    recorder.error = "网络采集连接失败：" + str(error)[:200]
            history = await asyncio.wait_for(agent.run(max_steps=8), timeout=300)
            result.update(status="completed" if history.is_successful() else "error",
                          summary=redact(history.final_result() or "探索未得到最终结果"),
                          urls=history.urls(), actions=history.action_names())
        except Exception as error:
            history = agent.history
            result["summary"] = redact(f"{type(error).__name__}: {error}")[:1500] or "页面探索超过时间限制"
        finally:
            # 功能：在收尾阶段汇总截图、网络或浏览器清理错误，不覆盖已得到的探索结果。
            # 输入：stage 为失败阶段说明；error 为捕获的异常；result 是 run 外层的观察结果字典。
            # 输出/影响：返回 None；往 result.evidenceErrors 追加限长脱敏说明，随后外层统一写 observation.json。
            def evidence_error(stage, error):
                result.setdefault("evidenceErrors", []).append(stage + "：" + redact(str(error))[:200])
            try:
                if history:
                    result["urls"] = history.urls()
                    result["actions"] = history.action_names()
                    screenshots = [s for s in history.screenshots() if s]
                    if screenshots:
                        safe_path(relative+"/page.png", fetch=False).write_bytes(base64.b64decode(screenshots[-1]))
                        result["artifacts"].append({"name": "page.png", "url": f"/api/artifacts/{relative}/page.png"})
            except Exception as error:
                evidence_error("截图保存失败", error)
            try:
                await recorder.aclose()
            except Exception as error:
                evidence_error("网络记录收尾失败", error)
            if playwright:
                try:
                    await playwright.stop()
                except Exception as error:
                    evidence_error("采集连接关闭失败", error)
            try:
                await browser.kill()
            except Exception as error:
                evidence_error("浏览器关闭失败", error)
            result["network"] = session_summary(capture["id"], finish=True)
            result["path"] = relative+"/observation.json"
            write_file(result["path"], json.dumps(result, ensure_ascii=False, indent=2))
            update_record(task_id, lambda p: p.update(inspections=[*p.get("inspections", []), result]))
        return result
    return asyncio.run(run())
