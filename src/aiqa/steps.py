# 可视化用例编译器：把结构化步骤翻译成可读的 pytest Python 或 JMeter JMX。
# Step、compile_steps_details 都是项目定义；编译只是生成代码，实际执行在隔离测试容器中。

"""Visual steps compile to readable, versioned native assets."""
import ast
import json
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator

COMMON = {"set_variable", "sleep", "python", "if", "else", "repeat", "foreach", "end"}
ACTIONS = {
    "api": {"request", "extract", "assert"} | COMMON,
    "web": {"goto", "click", "dblclick", "hover", "fill", "press", "select", "check", "uncheck", "wait",
            "assert_visible", "assert_text", "assert_url", "assert_enabled", "assert_value", "assert_attribute",
            "assert_count", "assert_checked", "extract", "upload", "screenshot", "dialog", "open_tab", "switch_tab", "close_tab", "click_popup", "download", "reload", "back", "scroll", "drag"} | COMMON,
    "app": {"click", "fill", "back", "swipe", "wait", "assert_visible", "assert_text", "assert_enabled",
            "extract", "assert_attribute", "check", "uncheck", "assert_checked", "press", "clear", "screenshot"} | COMMON,
    "performance": {"request", "csv", "if", "else", "repeat", "once", "transaction", "end"},
}
LOCATOR_ACTIONS = {"click", "dblclick", "hover", "fill", "press", "select", "check", "uncheck", "wait",
                   "assert_visible", "assert_text", "assert_enabled", "assert_value", "assert_attribute",
                   "assert_count", "assert_checked", "upload", "click_popup", "download", "scroll", "drag", "clear"}

# 一条步骤的数据结构，由 Pydantic 检查类型、默认值和范围；多种测试类型共用它。
# action 决定本步骤做什么，只需填写该动作相关字段；method/path/by 等直接放在步骤对象里。
# HTTP 查询参数本身仍放 params 字典，请求头仍放 headers 字典，不要把所有动作字段都塞进 params。
# timeout 是毫秒级操作等待上限；不等同于整个 Run 的总超时。
class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    name: str = Field(default="", max_length=120)
    action: str = Field(description="API: request/extract/assert. Web: goto、元素操作、等待和断言。APP: 元素操作、等待、断言、back/swipe。api/web/app 通用 set_variable/sleep/python；性能 request/csv/if/else/repeat/once/transaction/end；api/web/app 支持 if/else/repeat/foreach/end。")
    enabled: bool = True
    timeout: int = Field(default=15000, ge=100, le=60000, description="毫秒。API 为 HTTPX 连接/读/写等阶段超时，不是整个请求的总耗时；UI 为操作/断言等待上限。")
    method: str = Field(default="GET", pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    path: str = Field(default="/", max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    body_type: str = Field(default="json", pattern="^(json|form|text|multipart)$")
    follow_redirects: bool | None = Field(default=None, description="留空沿用类型默认：API 不跟随，性能跟随。")
    source: str = Field(default="json", pattern="^(json|header|status|response_time|text|variable|element)$")
    expression: str = Field(default="", max_length=2000)
    query: str = Field(default="path", pattern="^(path|jmespath|jsonpath|regex)$", description="API 提取/断言查询语言：path=简单字段路径，jmespath、jsonpath 或正文 regex。")
    match_group: int = Field(default=0, ge=0, le=20, description="正则取第一个匹配的分组；0 为完整匹配。")
    variable: str = Field(default="", max_length=80)
    operator: str = Field(default="eq", pattern="^(eq|ne|contains|matches|gt|gte|lt|lte|exists|not_exists|length|type)$")
    expected: Any = None
    by: str = Field(default="css", pattern="^(css|role|label|text|testid|xpath|accessibility id|id|android uiautomator)$")
    selector: str = Field(default="", max_length=2000)
    role: str = Field(default="button", max_length=60)
    exact: bool = True
    text_exact: bool = False
    nth: int | None = Field(default=None, ge=0, le=1000, description="同名元素索引，从 0 开始；默认要求唯一元素。")
    frame: str = Field(default="", max_length=1000, description="Web iframe 的 CSS 选择器，留空为主页面。")
    value: str = Field(default="", max_length=6000)
    attribute: str = Field(default="", max_length=120, description="Web/APP 属性名称；extract 留空则读取元素文本。")
    state: str = Field(default="visible", pattern="^(visible|hidden|attached|detached)$", description="wait 等待元素状态。")
    duration_ms: int = Field(default=1000, ge=0, le=60000, description="仅 sleep 的固定暂停时间。优先使用条件等待。")
    python: str = Field(default="", max_length=10000, description="python 步骤的 Python 代码，直接写入测试函数；可用 rt.variables、rt.value，以及 page/driver（相应类型）。与原生脚本具有同样执行权限。")
    direction: str = Field(default="up", pattern="^(up|down|left|right)$")
    expected_status: int | None = Field(default=None, ge=100, le=599)
    body_pattern: str = Field(default="", max_length=2000)
    extract_json: dict[str, str] = Field(default_factory=dict, description="仅性能请求：变量名 → JMeter JSONPath。")

    assert_json: dict[str,Any] = Field(default_factory=dict, description="请求内 JSONPath 到期望值的断言映射，API 和性能通用。")
    reason: str = Field(default="", max_length=1000, description="python 局部扩展必须说明标准动作尚不能表达的逻辑。")
    count: int = Field(default=1, ge=1, le=1000, description="repeat 的次数；循环索引从 0 起，可选保存至 variable。")
    items: Any = Field(default=None, description="foreach 的数组或 {{变量}}，每项写入 variable；最多 1000 项。")
    condition: str = Field(default="visible", pattern="^(visible|hidden|exists|enabled|checked|text)$", description="if source=element 的判断类型；有界等待 timeout 毫秒，预期值由 expected 或 value 指定。")
    tab: str = Field(default="main", max_length=80, description="Web 标签页名称；初始为 main。")
    target_selector: str = Field(default="", max_length=2000, description="Web drag 的目标 CSS 选择器，与源元素使用相同 iframe。")
    uploads: dict[str,str] = Field(default_factory=dict, description="API multipart：表单文件字段名→辅助文件名；正文为其他表单字段。")
    columns: str = Field(default="", max_length=1000, description="CSV 变量名，逗号分隔；文件名使用 value。")
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    ignore_first_line: bool = True
    recycle: bool = True
    stop_thread: bool = False
    share_mode: str = Field(default="all", pattern="^(all|thread)$")
    max_response_ms: int | None = Field(default=None, ge=1, le=60000, description="性能单次请求耗时断言；区别于整个运行的 P95 阈值。")

    @model_validator(mode="before")
    @classmethod
    def condition_default_source(cls, data):
        if isinstance(data,dict) and data.get("action") == "if" and "source" not in data:
            return dict(data,source="variable")
        return data

    @model_validator(mode="after")
    def numeric_protocol_values(self):
        if self.action in {"assert", "if"} and self.source in {"status", "response_time"} and self.operator in {"eq", "ne", "gt", "gte", "lt", "lte"}:
            # References are resolved at execution time.
            if not (isinstance(self.expected, str) and "{{" in self.expected):
                try:
                    self.expected = int(self.expected) if self.source == "status" else float(self.expected)
                except (ValueError, TypeError):
                    raise ValueError("HTTP 状态和响应时间的期望值必须是数字")
        return self

# 先补齐 ID/默认值，再检查动作是否适用于当前类型、定位器/查询是否合法及控制块是否配对。
# 这些检查在保存前发现明显配置错误；通过校验不代表真实接口或页面一定符合预期。
def validate_steps(kind, steps):
    if kind not in ACTIONS:
        raise ValueError("不支持的步骤测试类型")
    if not 1 <= len(steps) <= 100:
        raise ValueError("每个用例需要 1～100 个步骤")
    from .step_blocks import active_step_indices
    active = active_step_indices(steps)
    normalized, ids = [], set()
    for i, raw in enumerate(steps):
        s = Step.model_validate(raw)
        if s.action not in ACTIONS[kind]:
            raise ValueError(f"第 {i+1} 步的操作不适用于 {kind}")
        s.id, s.name = s.id or f"step-{i+1}", s.name or f"步骤 {i+1}"
        if s.id in ids:
            raise ValueError(f"第 {i+1} 步的 ID 重复；复制步骤应生成新的 ID")
        ids.add(s.id)
        if kind == "performance" and s.expected_status is None:
            s.expected_status = 200
        if kind == "api" and s.extract_json:
            raise ValueError("API 请用 extract 步骤提取变量，extract_json 用于性能计划")
        if i in active:
            if s.source == "element" and s.action != "if": raise ValueError("source=element 仅用于元素条件 if")
            if s.action in {"extract", "set_variable"} and not s.variable.isidentifier():
                raise ValueError(f"第 {i+1} 步需要有效的变量名称")
            needs_locator = s.action in LOCATOR_ACTIONS or (s.action in {"extract", "assert_attribute"} and kind in {"web", "app"})
            if kind == "app" and s.action == "press": needs_locator = False
            if kind in {"web", "app"} and needs_locator and not s.selector:
                raise ValueError(f"第 {i+1} 步需要元素定位信息")
            if kind == "web" and needs_locator and s.by in {"accessibility id", "id", "android uiautomator"}:
                raise ValueError("Web 使用 css/role/label/text/testid/xpath")
            if kind == "app" and needs_locator and s.by not in {"accessibility id", "id", "xpath", "android uiautomator"}:
                raise ValueError("APP 使用 accessibility id/id/xpath/android uiautomator")
            if s.action in {"request", "goto", "open_tab"}:
                variable_path = kind in {"api", "web"} and bool(re.fullmatch(r"\{\{[A-Za-z_][A-Za-z0-9_.\[\]-]*\}\}", s.path))
                if not variable_path and (not s.path.startswith("/") or s.path.startswith("//")):
                    raise ValueError("路径必须是当前环境内的相对路径，例如 /api/login；API/Web 也可引用保存了相对路径的完整变量")
            if kind=="app" and (s.nth is not None or s.frame):raise ValueError("APP 不支持 Web iframe 或 nth 定位选项")
            if s.action == "request" and s.body_type == "form" and s.body is not None and not isinstance(s.body, dict):
                raise ValueError("表单请求体需要键值对象")
            if s.action in {"assert_attribute"} and not s.attribute:
                raise ValueError("属性断言需要填写属性名称")
            if s.action == "assert_count":
                try: s.expected = int(s.expected)
                except (TypeError, ValueError): raise ValueError("元素数量需要整数")
                if s.expected < 0 or s.nth is not None:
                    raise ValueError("元素数量不能为负；数量断言不要同时选择第几个元素")
            if s.action in {"upload", "screenshot", "download"}:
                from pathlib import PurePosixPath
                if not s.value or PurePosixPath(s.value).name != s.value or "\\" in s.value or s.value in {".", ".."}:
                    raise ValueError("上传文件或截图使用简单文件名，不允许目录路径")
                if s.action == "screenshot" and not s.value.endswith(".png"):
                    raise ValueError("截图文件名必须以 .png 结尾")
            if s.action == "dialog" and s.value not in {"accept", "dismiss"}:
                raise ValueError("弹窗处理选择 accept 或 dismiss")
            if s.action == "python":
                if not s.python.strip(): raise ValueError("Python 步骤不能为空")
                tree = ast.parse("def test_step():\n" + "\n".join("    " + line for line in s.python.splitlines()))
                if any(isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) for n in ast.walk(tree)):
                    raise ValueError("Python 步骤不要使用 return/yield 提前终止用例")
            if (kind == "api" and s.action in {"extract", "assert", "if"}) or (s.action == "if" and s.source == "variable" and kind in {"web", "app"}):
                allowed={"json":{"path","jmespath","jsonpath"},"variable":{"path","jmespath","jsonpath"},"text":{"path","regex"}}
                if s.query not in allowed.get(s.source,{"path"}):raise ValueError("查询方式不适用于所选数据来源")
                if "{{" not in s.expression:
                    try:
                        if s.query == "jmespath":
                            import jmespath
                            jmespath.compile(s.expression)
                        if s.query == "jsonpath":
                            from jsonpath_ng.ext import parse
                            parse(s.expression)
                        if s.query == "regex":re.compile(s.expression)
                    except Exception as error:raise ValueError(f"第 {i+1} 步的 {s.query} 表达式无效：{error}") from None
            if s.body_pattern: re.compile(s.body_pattern)
            for expression in s.assert_json:
                if not expression.startswith("$"):raise ValueError("assert_json 使用以 $ 开头的 JSONPath")
                from jsonpath_ng.ext import parse
                parse(expression)
            if kind == "performance" and s.action == "if":
                if s.source != "variable" or not s.expression.isidentifier() or s.query != "path":raise ValueError("性能条件使用 source=variable，expression 为 JMeter 变量名，query=path")
                if s.operator in {"type","length"}:raise ValueError("性能条件比较变量值，暂不提供 type/length")
            if s.action == "if":
                if s.source == "element":
                    if kind not in {"web", "app"} or not s.selector: raise ValueError("元素条件需要 Web/APP 定位器")
                    allowed = {"accessibility id", "id", "xpath", "android uiautomator"} if kind == "app" else {"css", "role", "label", "text", "testid", "xpath"}
                    if s.by not in allowed: raise ValueError("元素条件定位方式不适用于当前类型")
                elif kind != "api" and s.source != "variable": raise ValueError("Web/APP 条件使用 source=variable 或 element")
            if s.action == "foreach" and (not s.variable.isidentifier() or not (isinstance(s.items, list) or isinstance(s.items, str))): raise ValueError("foreach 需要 variable 和数组 items 或 {{数组变量}}")
            if s.action == "foreach" and isinstance(s.items, list) and len(s.items)>1000: raise ValueError("foreach 最多 1000 项")
            if s.action == "repeat" and s.variable and not s.variable.isidentifier(): raise ValueError("循环索引变量名称无效")
            if s.action in {"open_tab", "switch_tab", "close_tab", "click_popup"} and not s.tab.isidentifier(): raise ValueError("标签页名称需要有效标识符")
            if s.action == "drag" and not s.target_selector: raise ValueError("拖拽需要 target_selector 目标 CSS 定位器")
            if kind == "performance" and s.body_type == "multipart": raise ValueError("性能暂不支持 multipart；需要原生 HTTP 文件上传组件时走明确回退")
            if s.action == "csv":
                from pathlib import PurePosixPath
                if not s.value or PurePosixPath(s.value).name != s.value or "\\" in s.value: raise ValueError("CSV 使用辅助文件中的简单文件名")
                if not s.columns or not all(v.strip().isidentifier() for v in s.columns.split(",")): raise ValueError("CSV columns 需要逗号分隔的有效变量名")
            if s.body_type == "multipart":
                if kind != "api" or (s.body is not None and not isinstance(s.body,dict)): raise ValueError("multipart 正文需要表单键值对象")
                from pathlib import PurePosixPath
                if not s.uploads or any(PurePosixPath(v).name != v or "\\" in v or v in {".",".."} for v in s.uploads.values()): raise ValueError("multipart uploads 需要字段名→辅助文件名")
        normalized.append(s.model_dump())
    if not any(normalized[i]["action"] not in {"end","else"} for i in active):
        raise ValueError("至少需要一个启用的步骤")
    from .step_blocks import block_links
    block_links(normalized)
    return normalized

def py(value):
    return repr(value)

# 把 by/selector 等定位配置生成 Playwright Locator 表达式。
# role、label 是语义定位；frame 先切 iframe；nth 指多个匹配中的第几个，从 0 开始。
def locator_code(s):
    target = f"page.frame_locator(rt.value({py(s['frame'])}))" if s["frame"] else "page"
    selector = f"rt.value({py(s['selector'])})"
    if s["by"] == "role": text = f"{target}.get_by_role({py(s['role'])}, name={selector}, exact={s['exact']})"
    elif s["by"] in {"label", "text"}: text = f"{target}.get_by_{s['by']}({selector}, exact={s['exact']})"
    elif s["by"] == "testid": text = f"{target}.get_by_test_id({selector})"
    else: text = f"{target}.locator(" + (f"'xpath=' + {selector}" if s["by"] == "xpath" else selector) + ")"
    return text + (f".nth({s['nth']})" if s["nth"] is not None else "")

# 单步翻译：API 生成 HTTPX 请求与断言，Web 生成 Playwright 动作，APP 生成 WebDriver 操作。
# 这里只返回代码行，不在平台上发请求或点击页面；rt 是生成脚本里的 StepRuntime 实例。
# 引用值在运行时由 rt.value 替换，因此后面请求可使用前面刚提取到的 token。
def step_code(kind, s):
    action, timeout = s["action"], s["timeout"]
    value = f"rt.value({py(s['value'])})"
    expected = f"rt.value({py(s['expected'])})"
    if action == "set_variable": return [f"rt.variables[{py(s['variable'])}] = {expected}"]
    if action == "sleep": return [f"time.sleep({s['duration_ms']} / 1000)"]
    if action == "python": return s["python"].splitlines()
    # 下面拼的是未来在测试容器执行的源码字符串，不是在当前保存请求里执行 HTTP。
    if kind == "api":
        if action == "request":
            opts = [f"headers=rt.value({py(s['headers'])})", f"params=rt.value({py(s['params'])})",
                    f"timeout={timeout} / 1000", f"follow_redirects={bool(s['follow_redirects'])}"]
            if s["body"] is not None:
                opts.append({"json": "json", "form": "data", "text": "content", "multipart":"data"}[s["body_type"]] + f"=rt.value({py(s['body'])})")
            if s["body_type"] == "multipart": opts.append(f"uploads=rt.value({py(s['uploads'])})")
            lines = [f"response = rt.request({py(s['method'])}, rt.url(rt.value({py(s['path'])})),",
                     "                      " + ", ".join(opts) + ")"]
            if s["expected_status"] is not None: lines.extend([f"rt.active['expectedStatus'] = {s['expected_status']}", f"assertion(response.status_code, 'eq', {s['expected_status']})"])
            if s["body_pattern"]: lines.append(f"assertion(response.text, 'matches', {py(s['body_pattern'])})")
            for expression,expected_json in s["assert_json"].items():
                lines.append(f"assertion(rt.read('json', {py(expression)}, query='jsonpath'), 'eq', rt.value({py(expected_json)}))")
            return lines
        read = f"rt.read({py(s['source'])}, rt.value({py(s['expression'])}), query={py(s['query'])}, group={s['match_group']})"
        if action == "extract": return [f"rt.variables[{py(s['variable'])}] = {read}", f"rt.require_variable({py(s['variable'])})"]
        return [f"assertion({read}, {py(s['operator'])}, {expected})"]
    if kind == "web":
        if action == "open_tab": return [f"page = rt.open_tab({py(s['tab'])})", f"page.goto(rt.url(rt.value({py(s['path'])})), timeout={timeout})"]
        if action == "switch_tab": return [f"page = rt.switch_tab({py(s['tab'])})"]
        if action == "close_tab": return [f"page = rt.close_tab({py(s['tab'])})"]
        if action == "reload": return [f"page.reload(timeout={timeout})"]
        if action == "back": return [f"page.go_back(timeout={timeout})"]
        if action == "goto": return [f"page.goto(rt.url(rt.value({py(s['path'])})), timeout={timeout})"]
        if action == "assert_url": return [f"expect(page).to_have_url(re.compile({value}), timeout={timeout})"]
        if action == "screenshot": return [f"page.screenshot(path={py('/work/' + s['value'])}, full_page=True, timeout={timeout})"]
        if action == "dialog":
            return ["# 处理下一个弹窗；放在触发弹窗的点击之前。",
                    f"page.once('dialog', lambda dialog: dialog.{s['value']}())"]
        lines = [f"locator = {locator_code(s)}"]
        if action == "click_popup": lines.append(f"page = rt.click_popup(locator, {py(s['tab'])}, {timeout})")
        elif action == "download": lines.append(f"rt.download(locator, {value}, {timeout})")
        elif action == "scroll": lines.append(f"locator.scroll_into_view_if_needed(timeout={timeout})")
        elif action == "drag":
            target = f"page.frame_locator(rt.value({py(s['frame'])}))" if s["frame"] else "page"
            lines.append(f"locator.drag_to({target}.locator(rt.value({py(s['target_selector'])})), timeout={timeout})")
        elif action in {"click", "dblclick", "hover", "check", "uncheck"}: lines.append(f"locator.{action}(timeout={timeout})")
        elif action in {"fill", "press"}: lines.append(f"locator.{action}({value}, timeout={timeout})")
        elif action == "select": lines.append(f"locator.select_option({value}, timeout={timeout})")
        elif action == "upload": lines.append(f"locator.set_input_files(rt.file({value}), timeout={timeout})")
        elif action == "wait": lines.append(f"locator.wait_for(state={py(s['state'])}, timeout={timeout})")
        elif action == "assert_visible": lines.append(f"expect(locator).{'not_to_be_visible' if s['expected'] is False else 'to_be_visible'}(timeout={timeout})")
        elif action == "assert_enabled": lines.append(f"expect(locator).{'to_be_disabled' if s['expected'] is False else 'to_be_enabled'}(timeout={timeout})")
        elif action == "assert_checked": lines.append(f"expect(locator).{'not_to_be_checked' if s['expected'] is False else 'to_be_checked'}(timeout={timeout})")
        # expect 断言会在超时内重试条件；它与立即比较一次文本或固定 sleep 不同。
        elif action == "assert_text": lines.append(f"expect(locator).{'to_have_text' if s['text_exact'] else 'to_contain_text'}({value}, timeout={timeout})")
        elif action == "assert_value": lines.append(f"expect(locator).to_have_value({value}, timeout={timeout})")
        elif action == "assert_count": lines.append(f"expect(locator).to_have_count({s['expected']}, timeout={timeout})")
        elif action == "assert_attribute": lines.append(f"expect(locator).to_have_attribute({py(s['attribute'])}, {value}, timeout={timeout})")
        elif action == "extract":
            call = f"input_value(timeout={timeout})" if s["attribute"]=="value" else f"get_attribute({py(s['attribute'])}, timeout={timeout})" if s["attribute"] else f"inner_text(timeout={timeout})"
            lines.extend([f"rt.variables[{py(s['variable'])}] = locator.{call}", f"rt.require_variable({py(s['variable'])})"])
        return lines
    # APP helpers keep driver operations explicit and share the platform fixture.
    if action == "back": return ["driver.back()"]
    if action == "press": return [f"rt.press_key({value})"]
    if action == "screenshot": return [f"driver.save_screenshot({py('/work/' + s['value'])})"]
    if action == "swipe": return [f"rt.swipe({py(s['direction'])})"]
    loc = py(("-android uiautomator" if s["by"] == "android uiautomator" else s["by"], s["selector"]))
    lines = [f"target = rt.value({loc})", f"wait = WebDriverWait(driver, {timeout} / 1000)"]
    if action == "wait":
        condition = {"visible":"visibility_of_element_located", "hidden":"invisibility_of_element_located",
                     "attached":"presence_of_element_located", "detached":None}[s["state"]]
        lines.append(f"wait.until(EC.{condition}(target))" if condition else "wait.until(lambda d: not d.find_elements(*target))")
    elif action == "assert_visible":
        lines.append(f"wait.until(EC.{'invisibility_of_element_located' if s['expected'] is False else 'visibility_of_element_located'}(target))")
    elif action in {"click", "fill"}:
        lines.append("element = wait.until(EC.element_to_be_clickable(target))")
        lines.extend(["element.click()"] if action == "click" else ["element.clear()", f"element.send_keys({value})"])
    elif action == "assert_text":
        lines.append(f"wait.until(lambda d: d.find_element(*target).text == {value})" if s["text_exact"] else f"wait.until(lambda d: {value} in d.find_element(*target).text)")
    elif action == "assert_enabled": lines.append(f"wait.until(lambda d: d.find_element(*target).is_enabled() == {s['expected'] is not False})")
    elif action in {"check", "uncheck"}:
        lines.extend(["element = wait.until(EC.element_to_be_clickable(target))", f"if element.get_attribute('checked') != {py(str(action=='check').lower())}: element.click()", f"wait.until(lambda d: d.find_element(*target).get_attribute('checked') == {py(str(action=='check').lower())})"])
    elif action == "assert_checked": lines.append(f"wait.until(lambda d: d.find_element(*target).get_attribute('checked') == {py(str(s['expected'] is not False).lower())})")
    elif action == "clear": lines.extend(["element = wait.until(EC.element_to_be_clickable(target))", "element.clear()"])
    elif action == "assert_attribute": lines.append(f"wait.until(lambda d: d.find_element(*target).get_attribute({py(s['attribute'])}) == {value})")
    elif action == "extract":
        lines.append("element = wait.until(EC.presence_of_element_located(target))")
        lines.append(f"rt.variables[{py(s['variable'])}] = " + (f"element.get_attribute({py(s['attribute'])})" if s["attribute"] else "element.text"))
        lines.append(f"rt.require_variable({py(s['variable'])})")
    return lines

# 完整编译入口，返回三个结果：规范化步骤、可执行源码、步骤到源码行号的映射。
# performance 转 JMX；其他类型生成 test_visual_case，并为每一步包上 rt.step 记录执行证据。
# 最后 ast.parse 只确认 Python 语法成立，不会执行其中的测试代码。
def compile_steps_details(kind, steps):
    steps = validate_steps(kind, steps)
    mapping = []
    if kind == "performance":
        from .visual_performance import compile_plan
        return steps, *compile_plan(steps)
    args = {"api":"base_url", "web":"page, base_url", "app":"driver, base_url"}[kind]
    lines = ["# Generated from AIQA visual steps. Each block maps to one step.",
             "import re", "import time", "from aiqa.runners.steps import StepRuntime, assertion"]
    if kind == "web": lines.append("from playwright.sync_api import expect")
    if kind == "app": lines.extend(["from selenium.webdriver.support.ui import WebDriverWait", "from selenium.webdriver.support import expected_conditions as EC"])
    lines.extend(["", f"def test_visual_case({args}):",
                  "    with StepRuntime(base_url" + (", page=page" if kind=="web" else ", driver=driver" if kind=="app" else "") + ") as rt:"])
    from .step_blocks import block_links
    links = block_links(steps)
    # 输出一步对应的源码，同时记住起止行号；stepMap 使报告/编辑器能从步骤定位到生成代码。
    def emit(index, indent, body):
        s = steps[index]
        start = len(lines)+1
        lines.append(" "*indent + f"# STEP {index+1} {s['id']}: {s['name'].replace(chr(10),' ').replace(chr(13),' ')}")
        info = dict(index=index+1,id=s["id"],name=s["name"],action=s["action"],sourceLine=start+1)
        if not s["enabled"]:
            lines.append(" "*indent + f"rt.skip({py(info)})")
        else:
            lines.append(" "*indent + f"with rt.step({py(info)}):")
            lines.extend(" "*(indent+4)+line for line in (body or ["pass"]))
        mapping.append(dict(id=s["id"],index=index+1,name=s["name"],start=start,end=len(lines),enabled=s["enabled"]))
        return info
    def skip_range(lo, hi, indent):
        for k in range(lo,hi):
            s = steps[k]
            lines.append(" "*indent + f"rt.skip({py(dict(index=k+1,id=s['id'],name=s['name'],action=s['action']))})")
    # 递归展开 if/else、repeat、foreach 控制块，生成真正的 Python 分支与循环。
    # 没走到的分支也记录 skipped；禁用的控制块整段跳过，不能只禁用标题却运行内部动作。
    def walk(lo, hi, indent=8):
        i=lo
        while i<hi:
            s=steps[i];action=s["action"]
            if i in links:
                end, alt=links[i]
                if not s["enabled"]:
                    # Compile the disabled subtree normally under a false guard, preserving its source map.
                    emit(i,indent,["pass"])
                    lines.append(" "*indent+"if False:")
                    walk(i+1,alt if alt is not None else end,indent+4)
                    if alt is not None:
                        emit(alt,indent+4,["pass"]);walk(alt+1,end,indent+4)
                    lines.append(" "*indent+"else:");skip_range(i+1,end,indent+4)
                elif action == "if":
                    variable = f"_aiqa_condition_{i}"
                    if s["source"] == "element":
                        locator=locator_code(s) if kind=="web" else py(("-android uiautomator" if s["by"]=="android uiautomator" else s["by"],s["selector"]))
                        expr=f"rt.element_condition({locator}, {py(s['condition'])}, {s['timeout']}, rt.value({py(s['expected'])}), rt.value({py(s['value'])}))"
                    else:
                        expr=f"rt.condition({py(s['source'])}, rt.value({py(s['expression'])}), {py(s['query'])}, {py(s['operator'])}, rt.value({py(s['expected'])}), group={s['match_group']})"
                    emit(i,indent,[variable+" = "+expr]);lines.append(" "*indent+f"if {variable}:")
                    walk(i+1,alt if alt is not None else end,indent+4)
                    if i+1 == (alt if alt is not None else end):lines.append(" "*(indent+4)+"pass")
                    if alt is not None:skip_range(alt+1,end,indent+4)
                    lines.append(" "*indent+"else:")
                    skip_range(i+1,alt if alt is not None else end,indent+4)
                    if alt is not None:emit(alt,indent+4,["pass"]);walk(alt+1,end,indent+4)
                    elif i+1==end:lines.append(" "*(indent+4)+"pass")
                else:
                    values=f"list(range({s['count']}))" if action=="repeat" else f"rt.value({py(s['items'])})"
                    local=f"_aiqa_values_{i}"
                    emit(i,indent,[f"{local} = rt.loop_values({values})"])
                    lines.append(" "*indent+f"for _aiqa_item_{i} in {local}:")
                    if s["variable"]:lines.append(" "*(indent+4)+f"rt.variables[{py(s['variable'])}] = _aiqa_item_{i}")
                    walk(i+1,end,indent+4)
                    if i+1==end:lines.append(" "*(indent+4)+"pass")
                emit(end,indent,["pass"]);i=end+1
            else:
                emit(i,indent,step_code(kind,s));i+=1
            lines.append("")
    walk(0,len(steps))
    mapping.sort(key=lambda m:m['index'])
    source = "\n".join(lines)+"\n"
    ast.parse(source)
    return steps, source, mapping

def compile_steps(kind, steps):
    normalized, source, _ = compile_steps_details(kind, steps)
    return normalized, source
