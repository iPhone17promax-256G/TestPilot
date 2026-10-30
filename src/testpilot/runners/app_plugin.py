# 测试容器中的 Appium driver fixture；设备租约已由外层执行器取得。
# 用例拿到 driver 操作远程 Appium，手机/模拟器并不运行在 pytest 进程内部。

"""APP fixture in the isolated pytest container."""
import hashlib,json,os
from pathlib import Path
import pytest

# 创建 WebDriver 会话后 yield 给测试；结束时保存截图、页面 XML、可用日志，再 quit。
# 隐式等待设为 0 是为了配合生成步骤里的显式 WebDriverWait，避免两种等待叠加。
# 会话 ID 落盘供异常清理使用，不能只靠测试正常结束时的 quit。
# 功能：创建测试专属的远程 Appium 会话，供生成 APP 用例使用并在结束时取证和退出。
# 输入：request 是 pytest 请求，提供测试 nodeid；Appium 地址和设备 capabilities 由 Runner 注入环境变量。
# 输出/影响：yield Appium driver 给测试；/work 写会话 ID、截图、页面 XML 与可用 logcat，finally 关闭远程会话。
@pytest.fixture
def driver(request):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    config=json.loads(os.environ["TESTPILOT_APP_CAPABILITIES"])
    remote=webdriver.Remote(os.environ["TESTPILOT_APPIUM_URL"],options=UiAutomator2Options().load_capabilities(config))
    remote.implicitly_wait(0)
    sessions=Path("/work/appium-sessions.json")
    values=json.loads(sessions.read_text()) if sessions.exists() else []
    sessions.write_text(json.dumps([*values,remote.session_id]))
    yield remote
    key=hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    try:
        remote.save_screenshot(f"/work/{key}-app.png")
        Path(f"/work/{key}-page.xml").write_text(remote.page_source)
        try:Path(f"/work/{key}-logcat.json").write_text(json.dumps(remote.get_log("logcat"),ensure_ascii=False)[-80000:])
        except Exception:pass
    except Exception as error:Path(f"/work/{key}-capture-error.txt").write_text(str(error))
    finally:remote.quit()
