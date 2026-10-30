# 学习导航：Test: Step2 是总编译入口，Test: Step3 只追 API 分支；这是生成脚本，不是执行测试。
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

    # 功能：在字段校验前给未指定来源的 if 步骤补上 variable，避免沿用响应 JSON 的默认来源。
    # 输入：cls 是 Pydantic 调用的 Step 类；data 为界面或 Agent 提交的原始步骤值。
    # 输出/影响：返回补上 source 的新字典，其他输入原样返回；不保存记录，也不读取运行时变量。
    @model_validator(mode="before")
    @classmethod
    def condition_default_source(cls, data):
        if isinstance(data,dict) and data.get("action") == "if" and "source" not in data:
            return dict(data,source="variable")
        return data

    # 功能：把 HTTP 状态和响应时间的比较期望值转换为数字，避免字符串与数字直接比较。
    # 输入：self 是 Pydantic 完成字段校验后的 Step 实例；只处理 assert/if、数字运算及 status/response_time 来源。
    # 输出/影响：返回 self，并可能修改 expected；含 {{变量}} 时延迟到运行时，无法转成数字则抛 ValueError；不执行请求。
    @model_validator(mode="after")
    def numeric_protocol_values(self):
        if self.action in {"assert", "if"} and self.source in {"status", "response_time"} and self.operator in {"eq", "ne", "gt", "gte", "lt", "lte"}:
            # 步骤字段中的 {{变量}} 留到运行时替换；保存时尚未执行登录等前置步骤，不能要求此时就取得 token 等真实值。
            if not (isinstance(self.expected, str) and "{{" in self.expected):
                try:
                    self.expected = int(self.expected) if self.source == "status" else float(self.expected)
                except (ValueError, TypeError):
                    raise ValueError("HTTP 状态和响应时间的期望值必须是数字")
        return self

# 先补齐 ID/默认值，再检查动作是否适用于当前类型、定位器/查询是否合法及控制块是否配对。
# 这些检查在保存前发现明显配置错误；通过校验不代表真实接口或页面一定符合预期。
# 功能：校验一组步骤是否能由所选引擎表达，补齐动作默认值、名称和稳定 ID。
# 输入：kind 为 api/web/app/performance；steps 为界面或 Agent 提供的扁平动作字典列表，控制块用 if/repeat 等与 end 配对。
# 输出/影响：返回规范化字典列表；校验动作、字段、查询语法、启用控制块和配对，错误抛异常；只在内存解析，不发请求、不写数据库。
def validate_steps(kind, steps):
    if kind not in ACTIONS:
        raise ValueError("不支持的步骤测试类型")
    if not 1 <= len(steps) <= 100:
        raise ValueError("每个用例需要 1～100 个步骤")
    from .step_blocks import active_step_indices
    active = active_step_indices(steps)
    # 先把界面或 Agent 提交的步骤逐个转为 Step，再补 ID 和名称。
    # 动作必须属于当前测试类型，ID 不能重复；最终返回统一字典，后面的编译器不再直接处理零散表单值。
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
        # 只对会执行的步骤检查必需参数：变量名、定位方式、请求路径、文件名和动作预期。
        # 禁用控制块内部也算停用，但字段类型和动作类型仍会经过前面的基础校验。
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
            # 响应提取、断言和分支要先确认“数据来源与查询方式”匹配，再检查查询表达式语法。
            # 包含 {{变量}} 的表达式留到执行时解析，因为保存时还没有前置请求产生的真实值。
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
            # 后面校验各类型特有能力：元素条件、循环变量、标签页名称、CSV 和文件上传。
            # 例如 APP 不接受 Web 的定位选项，性能请求也不能假装已经支持 multipart；不支持时明确报错。
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
    # 最后确认用例确实有启用的动作，且控制块都有配对的 end。
    # 通过后只返回规范化步骤；保存入口随后编译和写版本，校验阶段不会访问被测系统。
    if not any(normalized[i]["action"] not in {"end","else"} for i in active):
        raise ValueError("至少需要一个启用的步骤")
    from .step_blocks import block_links
    block_links(normalized)
    return normalized

# 功能：把一个配置值转换为可嵌入生成 Python 源码的字面量文本。
# 输入：value 是步骤字段或运行元信息，可为字符串、数字、字典、列表等 repr 可表示的值。
# 输出/影响：返回 repr(value) 字符串；只生成文字，不求值、不执行脚本，避免字符串直接拼接破坏源码引号。
def py(value):
    return repr(value)

# 把 by/selector 等定位配置生成 Playwright Locator 表达式。
# role、label 是语义定位；frame 先切 iframe；nth 指多个匹配中的第几个，从 0 开始。
# 功能：把 Web 步骤的定位字段翻译为 Playwright Locator 表达式文字。
# 输入：s 为规范化步骤字典；by/selector/role/exact 指定定位，frame 可选 iframe，nth 为零基匹配序号。
# 输出/影响：返回基于 page 的源码字符串；变量由未来 rt.value 解析，本函数不打开浏览器、不验证真实元素存在。
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
# 输入一条规范化步骤，返回该动作需要写入脚本的源码行列表。
# 功能：将一个普通动作翻译成 API、Web 或 APP 执行所需的源码行。
# 输入：kind 为 api/web/app；s 为 validate_steps 规范化的单步字典，含动作、定位、数据和毫秒 timeout。
# 输出/影响：返回字符串行列表，由编译器包成 rt.step 后保存；只生成 HTTPX/Playwright/Appium 等调用文字，此刻不执行动作、不保存文件。
def step_code(kind, s):
    # 先根据动作选翻译方式，并为 value/expected 生成“运行时才取值”的表达式。
    # 输出一直是源码文字；真正发请求、替换变量和保存结果，要等测试容器执行这份源码。
    action, timeout = s["action"], s["timeout"]
    value = f"rt.value({py(s['value'])})"
    expected = f"rt.value({py(s['expected'])})"
    # 设置变量、固定暂停和局部 Python 扩展是三种引擎共用动作，先在这里生成对应代码。
    # 变量存到未来的 rt.variables，只属于这次测试，不是写入项目全局配置。
    if action == "set_variable": return [f"rt.variables[{py(s['variable'])}] = {expected}"]
    if action == "sleep": return [f"time.sleep({s['duration_ms']} / 1000)"]
    if action == "python": return s["python"].splitlines()
    # Test: Step3 [开始] 第一遍只看这段 API 代码生成：request 发请求，extract 提取，assert 比较。
    # 概念：step_code 根据一条步骤配置返回对应 Python 源码行；生成源码中的 rt 指运行时的 StepRuntime 对象，而不是当前编译器对象。
    # 例如 request 会生成 rt.request(...)，extract 会生成 rt.variables[...] = rt.read(...)，assert 会生成 assertion(实际值,比较规则,期望值)。
    # {{token}} 是 TestPilot 的变量模板：编译时保留，测试运行到该行时由 rt.value 替换为前面提取的值；Python 本身不自动理解这套双花括号语法。
    # 职责：step_code 中的 API 分支，将一条请求、提取或断言动作翻译成未来执行的 Python 语句。
    # 输入：kind="api"；s 是单条已规范化步骤，包含 action、headers、params、path、query、预期值等字段。
    # 返回：list[str]，每项是一行待写入脚本的代码；不是 Response，也不是这条测试的执行结果。
    # 过程：request 生成 rt.request 和可选断言；extract 生成变量赋值；assert 生成取值与比较调用。
    # 数据：生成文本暂在内存，由 emit 加入完整 source，再由 case_save 保存；rt 属于之后运行的测试进程。
    # 边界：这里的字符串含 rt.value，变量将在执行时替换；保存阶段无法提前知道登录返回的 token。
    # 从这里进入接口步骤的编译；后面的 Web/APP 分支使用各自驱动。
    if kind == "api":
        # 请求动作把路径、请求头、查询参数、正文及文件上传配置组合成 rt.request 调用。
        # 这些配置可以引用前面提取的变量；运行时使用当前环境地址，毫秒 timeout 转成 HTTPX 使用的秒数。
        if action == "request":
            opts = [f"headers=rt.value({py(s['headers'])})", f"params=rt.value({py(s['params'])})",
                    f"timeout={timeout} / 1000", f"follow_redirects={bool(s['follow_redirects'])}"]
            if s["body"] is not None:
                opts.append({"json": "json", "form": "data", "text": "content", "multipart":"data"}[s["body_type"]] + f"=rt.value({py(s['body'])})")
            if s["body_type"] == "multipart": opts.append(f"uploads=rt.value({py(s['uploads'])})")
            lines = [f"response = rt.request({py(s['method'])}, rt.url(rt.value({py(s['path'])})),",
                     "                      " + ", ".join(opts) + ")"]
            # 请求步骤还可以自带状态码、正文正则和 JSON 字段断言，因此“请求发出”不等于“步骤成功”。
            # 生成的断言将在收到响应后比较实际值；不符合预期由运行时记为失败，源码行交回 emit 统一包裹。
            if s["expected_status"] is not None: lines.extend([f"rt.active['expectedStatus'] = {s['expected_status']}", f"assertion(response.status_code, 'eq', {s['expected_status']})"])
            if s["body_pattern"]: lines.append(f"assertion(response.text, 'matches', {py(s['body_pattern'])})")
            for expression,expected_json in s["assert_json"].items():
                lines.append(f"assertion(rt.read('json', {py(expression)}, query='jsonpath'), 'eq', rt.value({py(expected_json)}))")
            return lines
        # 提取和独立断言共用取值规则：source 选响应或变量，expression/query 决定怎么读取。
        # 提取把值放进 rt.variables 并检查是否取到；断言只比较实际值与期望，不会自动保存变量。
        read = f"rt.read({py(s['source'])}, rt.value({py(s['expression'])}), query={py(s['query'])}, group={s['match_group']})"
        if action == "extract": return [f"rt.variables[{py(s['variable'])}] = {read}", f"rt.require_variable({py(s['variable'])})"]
        return [f"assertion({read}, {py(s['operator'])}, {expected})"]
        # Test: Step3 [结束] 先回 Test: Step2 的 emit/walk，再回 Test: Step1 的 action(db) 完成版本保存。
        # 下一步：Test: Step4 → src/testpilot/testing.py 的 run_start；首次阅读可跳过下面 Web/APP 分支。
    # Web 分支把步骤翻译为 Playwright 操作：页面/标签页动作直接用 page，元素动作先生成 locator。
    # 断言使用 expect 在 timeout 内重试；元素提取结果仍进入 rt.variables，截图和下载写到运行目录。
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
        elif action == "assert_text": lines.append(f"expect(locator).{'to_have_text' if s['text_exact'] else 'to_contain_text'}({value}, timeout={timeout})")
        elif action == "assert_value": lines.append(f"expect(locator).to_have_value({value}, timeout={timeout})")
        elif action == "assert_count": lines.append(f"expect(locator).to_have_count({s['expected']}, timeout={timeout})")
        elif action == "assert_attribute": lines.append(f"expect(locator).to_have_attribute({py(s['attribute'])}, {value}, timeout={timeout})")
        elif action == "extract":
            call = f"input_value(timeout={timeout})" if s["attribute"]=="value" else f"get_attribute({py(s['attribute'])}, timeout={timeout})" if s["attribute"] else f"inner_text(timeout={timeout})"
            lines.extend([f"rt.variables[{py(s['variable'])}] = locator.{call}", f"rt.require_variable({py(s['variable'])})"])
        return lines
    # APP 分支生成 Appium driver 的操作，并用 WebDriverWait 等待元素或预期状态。
    # driver 由平台 fixture 准备，不在用例里重建；代码只生成动作，设备租约和运行证据由执行阶段处理。
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
# Test: Step2 [开始] 将 JSON 步骤校验后编译为 Python 源码，并建立步骤到源码的 stepMap。
# 职责：把结构化步骤编译成固定源码，并建立步骤与源码行号的对应表；调用者是 case_save。
# 输入：kind 为 api/web/app/performance；steps 是动作字典列表，先校验字段、补默认值和控制块配对。
# 返回：(steps, source, mapping)。steps 是规范化列表；source 是 Python/JMX 字符串；mapping 是 stepMap。
# 过程：emit 生成单步包裹层；walk 递归生成分支/循环；step_code 决定具体动作源码。
# 数据：这三项暂时只在编译器内存中，返回 case_save 后才写版本记录与源码文件；这里不写数据库。
# 边界：ast.parse 只检查生成代码的语法，不运行 HTTP/浏览器操作，也不保证依赖齐全或业务通过。
# 输入测试类型与步骤列表；返回规范化步骤、生成源码、步骤到源码的映射这三个值。
def compile_steps_details(kind, steps):
    # 概念：Step 是 TestPilot 定义的 Pydantic 数据类，表示界面上的一个动作，例如 request/click/extract/assert；与本教程阅读用的 Step1/Step2 不是一套编号。
    # validate_steps 把输入字典按 Step 字段校验、补默认值，检查 action 是否适用于 kind；通过后再 model_dump 为统一字典，供编译器使用。
    # 编译只是“把步骤翻译成代码字符串”，此时不执行生成代码。API/Web/APP 生成 pytest Python，性能生成 JMeter 的 JMX XML 测试计划。
    # 阶段一：校验输入并补默认值，确定每个动作的稳定 ID；mapping 用来记录它对应哪些源码行。
    # 性能用例交给 JMeter 编译器返回 JMX；其他类型继续生成 pytest 脚本，两条分支返回结构相同。
    steps = validate_steps(kind, steps)
    mapping = []
    if kind == "performance":
        from .visual_performance import compile_plan
        return steps, *compile_plan(steps)
    # 概念：fixture 是 pytest 按测试函数参数名提供的准备资源。运行时它把 BASE_URL 提供成 base_url，把 Playwright 页面提供成 page，把 Appium 驱动提供成 driver。
    # 下面 args/lines 是要写入未来脚本的文字，不是此刻已取得浏览器或手机对象；pytest 真正启动后才准备这些资源。
    # Playwright 是 Web 浏览器自动化库，Appium 通过 WebDriver 协议操作 Android；UI 运行和 HTTPX 接口请求是不同引擎，共享步骤与报告结构。
    # 阶段二：搭好整份测试脚本的外壳，按类型声明 pytest 需要提供的 base_url、page 或 driver。
    # 这些名字现在仍是脚本文字；只有容器执行时 fixture 才提供真实资源，StepRuntime 再管理共享变量和证据。
    args = {"api":"base_url", "web":"page, base_url", "app":"driver, base_url"}[kind]
    lines = ["# Generated from TestPilot visual steps. Each block maps to one step.",
             "import re", "import time", "from testpilot.runners.steps import StepRuntime, assertion"]
    if kind == "web": lines.append("from playwright.sync_api import expect")
    if kind == "app": lines.extend(["from selenium.webdriver.support.ui import WebDriverWait", "from selenium.webdriver.support import expected_conditions as EC"])
    lines.extend(["", f"def test_visual_case({args}):",
                  "    with StepRuntime(base_url" + (", page=page" if kind=="web" else ", driver=driver" if kind=="app" else "") + ") as rt:"])
    from .step_blocks import block_links
    # 阶段三：先取得控制块配对关系，再由下面三个内部函数组织源码。
    # emit 包裹单步、skip_range 记录未执行的步骤、walk 展开控制结构；它们共用内存中的 lines/mapping。
    links = block_links(steps)
    # 功能：为一步生成 rt.step 记录包裹层并建立这一步的源码映射。
    # 输入：index 为 steps 零基下标，indent 为生成源码空格数，body 为动作源码行；steps/lines/mapping 由外层编译函数闭包提供。
    # 输出/影响：返回步骤元信息字典；追加内存 lines 和 mapping，禁用步骤生成 skip；只编译文字，不在此执行或写版本。
    def emit(index, indent, body):
        # 先为这一步建立 ID、显示序号和源码起点，供运行报告回指界面上的原步骤。
        # 名称中的换行会被去掉，避免标题破坏脚本结构；这里的行号是生成脚本的位置，不是 Agent 的思考轮次。
        s = steps[index]
        start = len(lines)+1
        lines.append(" "*indent + f"# STEP {index+1} {s['id']}: {s['name'].replace(chr(10),' ').replace(chr(13),' ')}")
        info = dict(index=index+1,id=s["id"],name=s["name"],action=s["action"],sourceLine=start+1)
        # 启用动作放入 with rt.step，让执行时统一记录成功或异常；禁用动作只生成 rt.skip。
        # 随后把本步的源码区间加入 mapping；当前只改编译器内存，真正的步骤结果要等运行时产生。
        if not s["enabled"]:
            lines.append(" "*indent + f"rt.skip({py(info)})")
        else:
            lines.append(" "*indent + f"with rt.step({py(info)}):")
            lines.extend(" "*(indent+4)+line for line in (body or ["pass"]))
        mapping.append(dict(id=s["id"],index=index+1,name=s["name"],start=start,end=len(lines),enabled=s["enabled"]))
        return info
    # 功能：为未进入的分支补出 skipped 记录代码，让原始步骤在报告中仍可追踪。
    # 输入：lo/hi 为含 lo、不含 hi 的步骤下标区间；indent 为源码空格数，steps/lines 来自外层闭包。
    # 输出/影响：无返回值；向内存 lines 追加 rt.skip 源码，不在编译阶段创建执行结果或写报告文件。
    def skip_range(lo, hi, indent):
        # 把这个区间每条原步骤都编成 rt.skip 调用。
        # 这样分支没有被选中时，报告能明确显示“跳过了哪些步骤”，而不是让它们无声消失。
        for k in range(lo,hi):
            s = steps[k]
            lines.append(" "*indent + f"rt.skip({py(dict(index=k+1,id=s['id'],name=s['name'],action=s['action']))})")
    # 功能：递归展开普通步骤与 if/else、repeat、foreach 控制块，维持步骤和源码位置对应关系。
    # 输入：lo/hi 为含起点、不含终点的步骤区间；indent 为生成源码缩进，默认 8；kind/steps/links/lines/mapping 和 emit 均来自闭包。
    # 输出/影响：无返回值；递归追加源码行与映射，未走分支生成跳过记录；本函数不运行实际循环、不操作测试目标。
    def walk(lo, hi, indent=8):
        # 沿当前区间编译，遇到控制块就递归处理它的内部步骤，最后跳到对应 end 后继续。
        # 每个子块都使用配对表给出的边界，避免重复生成外层后续动作；产物仍是源码和映射。
        i=lo
        while i<hi:
            s=steps[i];action=s["action"]
            if i in links:
                end, alt=links[i]
                # 控制块被禁用时，内部源码和 stepMap 仍要保留，方便界面调整和报告定位。
                # 内部代码放在不会执行的分支中，运行时只写 skipped 记录，不能让“禁用循环”里的请求继续发出。
                if not s["enabled"]:
                    emit(i,indent,["pass"])
                    lines.append(" "*indent+"if False:")
                    walk(i+1,alt if alt is not None else end,indent+4)
                    if alt is not None:
                        emit(alt,indent+4,["pass"]);walk(alt+1,end,indent+4)
                    lines.append(" "*indent+"else:");skip_range(i+1,end,indent+4)
                # 条件块先生成一次条件判断，再生成真正的 Python if/else。
                # 条件来自响应、变量或页面/手机元素；选中分支执行动作，未选分支生成 skip，因此两边都能出现在报告里。
                elif action == "if":
                    variable = f"_testpilot_condition_{i}"
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
                    # 循环块使用固定次数或运行时数组生成 for；配置了 variable 就把当前项放入 rt.variables 给子步骤引用。
                    # 运行时还会检查循环数据类型和规模；保存时只是翻译循环，不会提前执行这些请求或界面动作。
                    values=f"list(range({s['count']}))" if action=="repeat" else f"rt.value({py(s['items'])})"
                    local=f"_testpilot_values_{i}"
                    emit(i,indent,[f"{local} = rt.loop_values({values})"])
                    lines.append(" "*indent+f"for _testpilot_item_{i} in {local}:")
                    if s["variable"]:lines.append(" "*(indent+4)+f"rt.variables[{py(s['variable'])}] = _testpilot_item_{i}")
                    walk(i+1,end,indent+4)
                    if i+1==end:lines.append(" "*(indent+4)+"pass")
                # 内部动作生成完后补上 end 步骤，并越过整个已处理控制块。
                # 普通动作则直接交给 step_code 翻译，再由 emit 加记录层；因此所有可视化动作走相同的证据入口。
                emit(end,indent,["pass"]);i=end+1
            else:
                emit(i,indent,step_code(kind,s));i+=1
            lines.append("")
    # 阶段四：编译完整列表、按界面顺序整理 stepMap，再检查生成 Python 的语法。
    # 返回步骤、源码、映射给 case_save；由它把元信息保存到 records 的版本 JSON，把源码写版本文件，此处不执行测试。
    walk(0,len(steps))
    mapping.sort(key=lambda m:m['index'])
    source = "\n".join(lines)+"\n"
    ast.parse(source)
    return steps, source, mapping
    # Test: Step2 [结束] 下一步：Test: Step3 → 本文件 step_code 的 kind == "api" 分支。
    # 那里给出每一步的代码，回来理解 emit 如何包上 with rt.step，再返回 Test: Step1 保存版本。

# 功能：提供只需要步骤和源码的兼容编译入口，省略完整编译器提供的映射。
# 输入：kind 为测试类型，steps 为待校验动作字典列表，直接交给 compile_steps_details。
# 输出/影响：返回 (规范化步骤列表, 源码字符串)；不返回 stepMap，不写数据库或版本文件，也不执行测试。
def compile_steps(kind, steps):
    normalized, source, _ = compile_steps_details(kind, steps)
    return normalized, source
