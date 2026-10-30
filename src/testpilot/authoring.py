# 内部 Agent 的可视化优先策略：除提示词外，还在保存入口验证输出形式。
# 局部 Python 扩展和完整原生脚本是两种不同回退，均要求说明原因。

"""Enforce the internal Agent's authoring policy, independent of model promises."""
import ast
import re
import xml.etree.ElementTree as ET

POLICY = "默认可视化：先读 steps_capabilities，再保存 steps；局部缺口用有理由的 python 步骤。整份原生脚本必须通过 cases_save_script 说明真实缺口或引用用户明确的脚本要求。"


# 功能：取得最新有效用户消息，用于核对原生脚本是否真正由用户要求。
# 输入：task 为 Task payload；messages 中 role=user 且未 suppressed 的消息参与筛选，旧记录无消息时使用 goal。
# 输出/影响：返回用户原始要求字符串；仅检查内存数据，不查询数据库、不接受助手生成的文字作为授权证据。
def user_text(task):
    messages = [m for m in task.get("messages", []) if m.get("role") == "user" and not m.get("suppressed")]
    return str(messages[-1].get("content", "")) if messages else str(task.get("goal", ""))


# 功能：检查 Agent 引用的原话是否确实要求使用原生脚本，并排除“不要脚本”等否定表达。
# 输入：task 为含用户消息的 Task 字典，quote 为模型提交的用户原话片段，必须出现在最新有效用户要求中。
# 输出/影响：返回布尔值；只做原文及正则核对，不保存结果、不调用模型，也不代表任意命令已获授权。
def explicit_script_request(task, quote):
    text = user_text(task)
    if not quote or quote not in text:
        return False
    start = text.index(quote)
    context = text[max(0, start-8):start] + quote
    if re.search(r"(?:不要|不用|别|禁止|不想|不希望|无需|don't|do not)[^。；;\n]{0,16}(?:脚本|代码|script|code|pytest|jmx)", context, re.I):
        return False
    return bool(re.search(r"(?:原生(?:脚本|代码|\s*jmx)|(?:写|用|生成|编写|修改|编辑|修复|保留|导入)[^。；;\n]{0,16}(?:脚本|代码|pytest|jmx)|(?:write|use|generate|edit|fix|import)[^.;\n]{0,24}(?:script|code|pytest|jmx))", quote, re.I))


# 整份脚本仅允许明确用户要求，或实际存在的 pytest 生命周期/JMeter 组件能力缺口。
# 检查引用是否出自用户消息，或代码是否真用到了所称缺口；不能只写一句“脚本更方便”。
# 功能：验证整份原生脚本的例外依据，允许明确用户要求或当前可视化能力确实覆盖不了的组件。
# 输入：kind 为测试类型，code 为整份源码/JMX，fallback 为 mode/reason/证据字典，task 用于核对本轮用户要求。
# 输出/影响：返回补上 policy 和可选 detectedComponents 的说明字典；证据不足抛 ValueError，仅解析源码/XML，不执行脚本或保存用例。
def native_fallback(kind, code, fallback, task):
    if not fallback or len(fallback.get("reason", "").strip()) < 12:
        raise ValueError(POLICY + "；回退理由至少 12 个字符，不能只说更方便。")
    mode = fallback.get("mode")
    if mode == "user_requested":
        if not explicit_script_request(task, fallback.get("user_quote", "")):
            raise ValueError("无法在本轮用户消息中确认明确的原生脚本要求；请使用可视化 steps。")
    elif mode == "capability_gap":
        capability = fallback.get("capability")
        if kind == "performance" and capability == "jmeter_component":
            root = ET.fromstring(code)
            covered = {"elementProp", "jmeterTestPlan", "hashTree", "TestPlan", "ThreadGroup", "HTTPSamplerProxy", "HeaderManager", "CookieManager", "ConstantTimer", "ResponseAssertion", "JSONPostProcessor", "CSVDataSet", "LoopController", "OnceOnlyController", "TransactionController", "DurationAssertion", "JSONPathAssertion", "IfController"}
            # 功能：递归收集 JMX 中启用的组件，防止禁用组件被拿来虚构可视化能力缺口。
            # 输入：tree 为当前 JMX hashTree；JMeter 用组件与其子 hashTree 成对表示结构，此生成器沿启用的配对递归。
            # 输出/影响：逐项 yield XML 节点供外层检查；不修改 XML、不运行 JMeter、不访问数据库。
            def enabled_nodes(tree):
                children = list(tree)
                for index in range(0,len(children)-1,2):
                    node, subtree = children[index:index+2]
                    if node.get("enabled", "true").lower() == "false":
                        continue
                    yield from node.iter()
                    yield from enabled_nodes(subtree)
            plan_tree = root.find("hashTree")
            if plan_tree is None: raise ValueError("JMX 需要 hashTree 结构")
            nodes = list(enabled_nodes(plan_tree))
            missing = sorted({n.tag for n in nodes if n.get("testclass") and n.tag not in covered and n.get("testclass") not in {"Arguments", "HTTPArguments", "HTTPArgument", "Argument", "Header"}})
            if any(n.tag == "stringProp" and n.get("name") == "File.path" and (n.text or "").strip() for n in nodes):
                missing.append("HTTPFileArgs")
            elif any(n.tag == "boolProp" and n.get("name") == "HTTPSampler.DO_MULTIPART_POST" and n.text == "true" for n in nodes):
                missing.append("multipart HTTP request")
            if not missing:
                raise ValueError("该 JMX 只使用可视化已覆盖的组件，请改用 steps；循环、CSV、事务和仅一次控制器都已支持。")
            fallback = dict(fallback, detectedComponents=missing)
        elif kind in {"api", "web", "app"} and capability == "pytest_lifecycle":
            tree = ast.parse(code)
            # 功能：识别源码 AST 装饰器是否属于 pytest.fixture，供外层检查真实被用到的生命周期扩展。
            # 输入：node 是函数装饰器 AST 节点，可以是 pytest.fixture 属性或 pytest.fixture(...) 调用。
            # 输出/影响：返回布尔值；只比较语法结构，不执行装饰器、不创建测试 fixture。
            def fixture(node):
                target = node.func if isinstance(node, ast.Call) else node
                return isinstance(target, ast.Attribute) and target.attr == "fixture" and isinstance(target.value, ast.Name) and target.value.id == "pytest"
            functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            fixtures = {}
            autouse = set()
            for function in functions:
                for decorator in function.decorator_list:
                    if not fixture(decorator):
                        continue
                    keywords = {k.arg:k.value for k in decorator.keywords} if isinstance(decorator,ast.Call) else {}
                    alias = keywords.get("name")
                    name = alias.value if isinstance(alias,ast.Constant) and isinstance(alias.value,str) else function.name
                    fixtures[name] = function
                    flag = keywords.get("autouse")
                    if isinstance(flag,ast.Constant) and flag.value is True:
                        autouse.add(name)
            tests = [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith("test_")]
            needed = set(autouse) if tests else set()
            for function in tests:
                needed.update(argument.arg for argument in function.args.args)
                for node in ast.walk(function):
                    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr == "getfixturevalue" and node.args and isinstance(node.args[0],ast.Constant):
                        needed.add(node.args[0].value)
                for decorator in function.decorator_list:
                    target = decorator.func if isinstance(decorator,ast.Call) else decorator
                    if isinstance(target,ast.Attribute) and target.attr == "usefixtures" and isinstance(decorator,ast.Call):
                        needed.update(a.value for a in decorator.args if isinstance(a,ast.Constant) and isinstance(a.value,str))
            if not fixtures.keys() & needed:
                raise ValueError("未发现被测试实际使用的自定义 fixture。仅声明闲置 fixture 或在测试文件里写 pytest_* hook 不能证明执行缺口；常规操作、分支和循环请使用 steps。")
        else:
            raise ValueError("整份脚本的能力缺口仅接受 pytest_lifecycle 或 jmeter_component；普通计算、拦截等局部逻辑用 python 步骤。")
    else:
        raise ValueError("回退 mode 必须为 user_requested 或 capability_gap")
    return dict(fallback, policy="visual-first-v1")


# 防止模型把整份脚本塞进一个 python 步骤伪装成可视化用例。
# 允许有理由的小段计算，但启用步骤中必须有真正的请求、界面动作或断言。
# 功能：防止把整份测试藏在一个 Python 步骤里绕过可视化优先，并要求局部代码扩展说明缺口。
# 输入：steps 为结构化动作字典列表，active_step_indices 决定实际启用子树；Python 扩展的 reason 来自步骤字段。
# 输出/影响：校验通过无返回值，缺乏真实动作或理由抛 ValueError；不编译、不执行、不持久化步骤。
def visual_authoring(steps):
    from .step_blocks import active_step_indices
    indices = active_step_indices(steps)
    active = [s for i,s in enumerate(steps) if i in indices]
    if not any(s.get("action") not in {"python", "sleep", "set_variable", "end", "else", "if", "repeat", "foreach"} for s in active):
        raise ValueError("不要把整份测试藏进 Python 步骤；至少使用一个真实请求、页面/设备操作或断言步骤。明确要求整份脚本时用 cases_save_script。")
    for s in active:
        if s.get("action") == "python" and len(s.get("reason", "").strip()) < 8:
            raise ValueError("Python 自定义步骤需要 reason 说明当前标准步骤不能表达的局部逻辑（至少 8 个字符）。")
