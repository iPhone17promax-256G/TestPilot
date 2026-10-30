# 学习导航：Test: Step8 管上下文/变量，Step9 真发请求，Step10 读响应，Step12 记录步骤结束。
# 此文件在测试容器运行；测试动作循环中的每一步都可能复用这些方法，编号只是推荐阅读顺序。
# 生成脚本运行时的助手：请求、变量、步骤记录和部分 UI 辅助操作。
# StepRuntime 是 TestPilot 自定义类，不是 Python/pytest 内置；此模块运行在测试容器中，不连接平台数据库。

"""Recording and variable helpers for generated native test code."""
import json
import os
import re
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path
from urllib.parse import urlparse
import httpx
import pytest

class Missing:
    # 功能：为“字段未找到”标记提供可读的调试文字，区别于合法 JSON null。
    # 输入：self 是项目创建的 MISSING 标记实例；repr 或错误信息展示时由 Python 调用。
    # 输出/影响：返回字符串 <字段缺失>，不读取业务数据，不产生存储或外部动作。
    def __repr__(self): return "<字段缺失>"
MISSING = Missing()

# 在已经拿到的 JSON 上查询字段：path 是简单路径，JMESPath/JSONPath 是第三方查询语言。
# 简单 path 查不到字段、JSONPath 无匹配时返回项目的 MISSING 标记；JMESPath 遵循该库规则，无匹配通常返回 None，不能把三种查询方式的空值行为说成完全相同。
# 功能：按简单路径、JMESPath 或 JSONPath 查询已经拿到的 JSON 数据。
# 输入：data 为解析后的响应/变量结构；expression 为查询表达式；query 指定查询语言，默认 path。
# 输出/影响：返回匹配值；path/JSONPath 无匹配返回 MISSING，JMESPath 遵循库规则通常返回 None；只处理内存。
def query_value(data, expression, query="path"):
    if not expression: return data
    if query == "jmespath":
        import jmespath
        return jmespath.search(expression, data)
    if query == "jsonpath":
        from jsonpath_ng.ext import parse
        matches = [match.value for match in parse(expression).find(data)]
        return MISSING if not matches else matches[0] if len(matches)==1 else matches
    from .steps import json_value
    try: return json_value(data, expression)
    except (KeyError, IndexError, TypeError): return MISSING

# 一份生成用例共享的执行上下文：保存最近响应、提取变量、浏览器页/设备以及步骤报告。
# 通过 with 管理 HTTPX Client 和收尾写盘；变量只属于当前这次用例执行。
# Test: Step8 [开始] 先看 __init__、__enter__ 和 value：建立本次用例的 Client、变量与步骤记录。
# 职责：创建整条可视化用例共享的运行上下文；生成脚本调用 with StepRuntime(...) as rt。
# 输入：base_url 是环境地址；page 是 pytest 提供的 Playwright 页面，driver 是 Appium 驱动，默认都为 None。
# 输入：report_path 是步骤报告路径，默认 /work/steps.json；self 是当前实例，由 Python 自动传入方法。
# 输出：StepRuntime(...) 构造对象（__init__本身返回None）；__enter__建Client并返回self，供脚本用rt访问。
# 内存：variables 存提取值，response 存最近 HTTPX 响应，records 存步骤结果，active 指向当前步骤。
# 文件：flush 将 records 写到 report_path，稍后由容器外 Runner 读取；这里不直接连接平台 MySQL。
# 边界：退出 with 会关闭 Client 并写最终报告；变量不跨用例共享，token 不等于用例库里的永久字段。
# 概念：StepRuntime 是 TestPilot 自定义的测试上下文类，不是 Python 或 HTTPX 自带功能。with StepRuntime(...) as rt 中的 rt 是它的实例。
# 它把本条用例的 HTTPX Client、最近响应、提取变量和步骤结果放在同一对象里，后面的动作可连续使用这些内存值。
# __enter__ 在进入 with 时创建 Client 并返回实例；__exit__ 在正常结束或异常退出时关闭 Client 并写报告，未吞异常则由 pytest 继续判失败。
# 这里的测试上下文属于一条用例运行，和 Agent state 的消息历史、账号身份 ContextVar 都不同。
class StepRuntime:
    # 创建一次用例共享的运行上下文；self 是这个对象，page/driver 由相应测试 fixture 提供。
    # 功能：初始化一条生成用例共享的运行对象，用来关联请求、变量、浏览器/设备与报告。
    # 输入：base_url 为测试地址；page/driver 为 pytest 提供的 Web/APP 驱动；report_path 为步骤 JSON 输出路径。
    # 输出/影响：构造函数返回 None；在 self 内存中建立状态，实际 Client 由 __enter__ 创建，报告稍后 flush 写盘。
    def __init__(self, base_url, page=None, driver=None, report_path="/work/steps.json"):
        # 把生成脚本传入的环境地址及可选页面/设备保存在本次 rt 对象；后续请求和 UI 动作共享它。
        # variables 装提取值，response 装最近响应，records 装动作结果，active 指向正在记录的动作，均先存在内存。
        # report_path 决定写盘位置，Web 最初页面登记为 main；operations 用来限制循环累计步数，这些状态不跨用例共享。
        self.base_url, self.page, self.driver = base_url, page, driver
        self.variables, self.records = {}, []
        self.response, self.elapsed, self.active = None, 0, None
        self.report_path = report_path
        self.tabs = {"main": page} if page else {}
        self.operations = 0

    # 执行 with StepRuntime(...) as rt 时调用，先准备客户端资源。
    # 功能：进入 with 时创建可复用连接与 Cookie 的同步 HTTPX Client。
    # 输入：self 是本条用例的 StepRuntime；生成脚本的 with 语句自动调用，无额外参数。
    # 输出/影响：返回 self 作为 rt；占用 HTTP 连接池资源，默认 30 秒超时且不跟随重定向。
    def __enter__(self):
        # 一次用例复用连接与 Cookie，默认不跟随重定向；具体请求可覆盖超时等选项。
        # 概念：HTTPX 是发送 HTTP/HTTPS 请求的 Python 库；Client 维护连接池和 Cookie，一条用例复用它可减少重复建连接，也能连续使用服务端设置的 Cookie。
        # 这里用的是同步 Client，client.request 会等待本次响应；HTTPX 支持异步不代表这个方法已经使用 AsyncClient。
        # 默认 timeout=30 秒、默认不跟随重定向；步骤字段的毫秒 timeout 在生成调用时换成秒传入，避免两个单位混用。
        self.client = httpx.Client(timeout=30, follow_redirects=False)
        return self

    # 收尾块：self 为本次上下文；*args 是Python自动传入的异常类型、异常对象、回溯，正常退出时都为None。
    # 输出：关闭Client并写最终steps.json；无返回值，因此不会吞掉with内的异常，pytest仍能记录失败。
    # 离开 with 块时自动调用，动作失败抛异常也会到这里；不返回 True，因此不会吞掉异常。
    # 功能：退出 with 时关闭 HTTP 连接池，并将最终动作记录写入步骤报告。
    # 输入：self 为本条用例上下文；args 是 Python 传入的异常类型、异常对象、回溯，正常退出均为空。
    # 输出/影响：返回 None，不吞掉测试异常；关闭连接并调用 flush 写 report_path，异常继续交给 pytest。
    def __exit__(self, *args):
        # 整条用例结束时释放连接池，并把最后一份动作记录写盘；正常结束或 with 内抛异常都走这里。
        # 没有吞掉异常，所以 pytest 仍能按真实失败判定，容器外 Runner 稍后读取报告文件。
        self.client.close()
        self.flush()

    # 递归替换 {{token}} 等引用；完整变量占满字符串时保留原类型，字符串拼接时才转成文本。
    # 辅助块 value：self 为本次上下文，value 可是文本、字典、列表、元组等；例如请求头里的 Bearer {{token}}。
    # 返回：递归替换后的值；纯 {{count}} 保留原类型，字符串拼接转文本。读内存 variables，不访问数据库。
    # 功能：替换步骤参数里的变量引用，元组定位器也保留元组类型。
    # 输入：value 为请求头、参数、字符串或定位元组；变量来源为 self.variables 和允许的测试凭据环境变量。
    # 输出/影响：返回替换后的值；不修改变量字典，不访问数据库；缺失引用会报错。
    def value(self, value):
        # 在动作真正使用参数时才从本次 variables 取值，例如后一请求的 Bearer {{token}} 引用前一步提取结果。
        # 定位元组仍保持元组；纯变量保持数字等原类型，拼进文本才转字符串；未提取就报错，避免悄悄带空值请求。
        from .steps import substitute
        if isinstance(value, tuple): return tuple(self.value(v) for v in value)
        return substitute(value, self.variables)
        # Test: Step8 [结束] 下一步：Test: Step9 → 本类 request；url 只负责组合当前环境的相对路径。
        # 暂时跳过文件/脱敏/界面辅助方法；步骤记录方法 step 在 Test: Step12 再回来看。

    # 只接受当前环境下的相对路径，拒绝变量把目标换成任意绝对 URL。
    # 这是可视化请求的目标检查，不能当成对任意原生 Python 脚本的网络防火墙。
    # 功能：拼接当前环境内的相对请求路径，防止可视化参数改成任意绝对地址。
    # 输入：path 必须以单个 / 开头，如 /api/login；base_url 来自构造时的环境配置。
    # 输出/影响：返回完整 URL 字符串；不发请求，拒绝无 / 或 // 路径，实际请求由 request 发送。
    def url(self, path):
        # 只让可视化请求使用当前环境里的 /路径；保留开头斜杠，避免 /http://... 被 urljoin 误解为另一台主机。
        # 这里组装 URL、不发请求；这是这条可视化入口的目标校验，不是任意 Python 脚本的网络防火墙。
        if not isinstance(path,str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("路径必须是当前环境内的相对路径")
        return self.base_url.rstrip("/") + path

    # 功能：检查生成动作引用的辅助上传文件，只接受简单文件名。
    # 输入：name 为用例辅助文件名；文件在测试进程当前工作目录，由外层 Runner 准备。
    # 输出/影响：返回已有文件的绝对路径；不创建或上传文件，缺失或非法名称时报错。
    def file(self, name):
        path = Path(name)
        if path.name != name or "\\" in name or name in {".",".."}: raise ValueError("上传文件使用简单文件名")
        if not path.is_file(): raise ValueError("请在辅助文件中添加 "+name)
        return str(path.resolve())

    # 记录步骤时替换已知测试凭据和名称像 token/password 的变量值。
    # 这是有限规则，不保证任意嵌套内容、截图或所有网络正文都已无敏感信息。
    # 功能：递归隐藏步骤结果中已知的测试凭据与敏感变量值。
    # 输入：value 为待报告的数据；self.variables 和 TEST_PASSWORD/TEST_TOKEN 提供有限的敏感值集合。
    # 输出/影响：返回替换后的结构，不修改原变量、不写文件；只替换已知字符串，不能保证任意证据完全脱敏。
    def sanitize(self, value):
        secrets = [str(v) for k,v in self.variables.items() if any(w in k.lower() for w in ["token","password","secret"]) and v]
        secrets += [os.environ.get(n,"") for n in ["TEST_PASSWORD","TEST_TOKEN"]]
        # 功能：作为 sanitize 的递归助手，在字典值、列表项和文本中替换外层 secrets。
        # 输入：item 为当前遍历的子数据；secrets 是外层汇总的本次用例敏感值。
        # 输出/影响：返回清理后的子数据；仅在内存中处理，数字等非文本值按原值保留。
        def clean(item):
            if isinstance(item,dict):return {k:clean(v) for k,v in item.items()}
            if isinstance(item,list):return [clean(v) for v in item]
            if isinstance(item,str):
                for secret in sorted(set(secrets),key=len,reverse=True):
                    if secret:item=item.replace(secret,"[REDACTED]")
            return item
        return clean(value)

    # 功能：将当前所有动作结果应用已有敏感值规则后写到步骤报告。
    # 输入：self.records 为本次用例的动作记录；self.report_path 为构造时指定的输出文件。
    # 输出/影响：返回 None；覆盖写步骤 JSON 文件，由容器外 Runner 读取，不直接写平台数据库。
    def flush(self):
        Path(self.report_path).write_text(json.dumps(self.sanitize(self.records), ensure_ascii=False))

    # 功能：为禁用的可视化步骤记录 skipped，并计入单次执行步数限制。
    # 输入：info 是编译器生成的步骤元信息，包含名称、动作、ID 等；self 为本次上下文。
    # 输出/影响：返回 None；追加内存记录并 flush 落盘，超过 5000 步时报错，不执行实际动作。
    def skip(self, info):
        self.operations += 1
        if self.operations > 5000: raise ValueError("单次用例最多执行或跳过 5000 个步骤")
        self.records.append(dict(info,status="skipped",duration=0))
        self.flush()

    # contextmanager 包住真实动作：进入记录 running，正常退出 passed，异常记录失败并继续抛出。
    # finally 不论成功失败都记录耗时并落盘，所以错误步骤也能在报告中定位。
    # 循环可能多次执行同一个步骤 ID，execution 表示本次执行序号，不能只按静态 ID 覆盖结果。
    # Test: Step12 [开始] 回到 with rt.step(...) 的包裹层：yield 执行动作，退出时记录通过或失败。
    # 职责：包住一个真实动作，记录 running→passed/failed/skipped、耗时与错误位置。
    # 输入：self 是运行上下文；info 是编译器生成的步骤元信息，含 id、index、name、action、sourceLine。
    # 使用/输出：with rt.step(info) 中执行请求/断言；step 是上下文管理器，不返回某个业务响应。
    # 内存：将记录加入 self.records，active 指向它；循环重复执行同一步时保留不同 execution 序号。
    # 文件：动作开始和结束都 flush 到 /work/steps.json，应用既有敏感值替换规则；不直接写平台数据库。
    # 边界：yield 将控制权交给 with 内动作，不开新线程；动作出错仍写失败证据，并继续抛异常给 pytest。
    # 将含 yield 的函数变成 with 管理器：yield 前准备，yield 后收尾。
    @contextmanager
    # info 是编译时生成的步骤 ID/名称/动作/源码行号，不是模型推理的 Step 编号。
    # 概念：@contextmanager 来自 Python 标准库，把含 yield 的函数变成 with 管理器；执行到 yield 时，把控制权交给 with 内的真实请求/断言。
    # 动作正常结束后从 yield 下一行继续，动作抛异常则在 yield 处进入 except，finally 无论哪条路径都记录耗时并写盘。这是同一线程的控制交接。
    # flush 将 self.records 序列化为 JSON 写到 report_path；并不把 HTTP Response 对象、全部提取变量或数据库 Session 直接序列化。
    # index/id 标识静态动作，execution 标识运行次数；循环第 2 次执行同一步仍留下第 2 条结果，而不是覆盖第 1 次。
    def step(self, info):
        # 动作开始：info 来自编译器，含静态步骤 ID、名称和源码位置；每次实际进入都另记执行序号，循环不会覆盖上一次。
        # 累计最多 5000 次，把 running 记录放到 records 并设为 active，再写 steps.json；进程途中停止也能看到开始到哪里。
        self.operations += 1
        if self.operations > 5000: raise ValueError("单次用例最多执行 5000 个步骤，检查循环次数")
        record = dict(info, status="running", execution=len(self.records)+1)
        self.records.append(record)
        self.active = record
        started=time.monotonic()
        self.flush()
        # 把控制权交给 with 里的真实动作：请求、提取或断言可以往 active 补证据；没有异常才标 passed。
        # pytest 的跳过另记 skipped，其他异常记失败位置和限长原因，再抛给 pytest；写一句“失败”不能代替真实异常。
        try:
            yield
            record["status"]="passed"
        except pytest.skip.Exception as error:
            record.update(status="skipped",error=str(error))
            raise
        except Exception as error:
            record.update(status="failed",error=str(error)[:4000])
            raise AssertionError(self.sanitize(f"第 {info['index']} 步「{info['name']}」失败：{error}")) from None
        # 动作收尾：不论通过、跳过还是失败都计算秒级耗时并写步骤 JSON，然后清掉 active，避免后续动作写错记录。
        # 这里只写测试容器的文件，完整测试结论稍后由 pytest 整体报告和容器外 Runner 共同判定。
        finally:
            record["duration"]=round(time.monotonic()-started,3)
            self.flush()
            self.active=None
        # Test: Step12 [结束] 下一步：Test: Step13 → src/testpilot/runners/pytest_plugin.py 的 pytest_sessionfinish。
        # 每一步都会如此收尾；__exit__ 关闭 HTTPX Client，pytest 完整报告与 steps.json 是两份证据。

    # HTTPX Client 真正发出 HTTP 请求；复用连接和 Cookie，并记录状态码与耗时。
    # 不在此猜业务是否成功；HTTP/JSON/业务效果的检查由后续断言明确表达。
    # Test: Step9 [开始] self.client.request 才是真正的 HTTPX 网络请求；最近响应保存到 self.response。
    # 职责：调用 HTTPX 真正发送请求，保存最近响应与基础执行信息；调用者是生成脚本里的 rt.request。
    # 输入：self 为当前运行上下文；method 如 GET/POST，url 为 rt.url 合成的完整地址。
    # 输入：**options 收集 headers、params、json/data/content、timeout（秒）等关键字参数；uploads 是项目上传配置。
    # 返回：HTTPX Response 对象，可读 status_code、headers、text、json()；不是平台统一响应字典。
    # 内存：response 留在 self.response 供后续提取；耗时记 self.elapsed；状态码/路径等写入当前 active 记录。
    # 存储：本函数不保存整份响应到数据库；步骤基础信息随后由 step/flush 写到 steps.json。
    # 边界：请求成功返回不等于业务正确；是否符合预期交给后续字段/状态/数据变化断言。
    # options 收集 headers/params/json/timeout 等关键字参数，再转给 HTTPX。
    def request(self, method, url, **options):
        # 请求准备：方法、地址和请求选项来自生成脚本，uploads 则是 TestPilot 的辅助文件配置，先转换为 HTTPX 的 multipart 文件参数。
        # 文件只能从本次 /work 中按简单名称读取，交 ExitStack 管理；普通请求不用打开文件，出错也会关闭已打开的句柄。
        started=time.monotonic()
        uploads = options.pop("uploads", {})
        # 管理数量不确定的文件句柄，退出时统一关闭，上传报错也会关闭。
        # 概念：ExitStack 是 Python 标准库的资源管理器，适合一次请求要打开数量不定的上传文件；enter_context 登记文件，退出 with 自动逐一关闭。
        # **options 把请求参数字典展开成 headers/params/json 等关键字参数；它只是 Python 传参语法，真正网络通信仍发生在下面的 HTTPX request。
        with ExitStack() as opened:
            if uploads:
                options["files"] = {field:(filename,opened.enter_context(open(self.file(filename),"rb"))) for field,filename in uploads.items()}
            # HTTPX 在这里真正把请求发给被测服务，等待响应后保存在 rt.response，下一条提取/断言直接读它。
            # 退出资源块关闭上传文件，连接池仍留给本条用例的其他请求复用。
            self.response=self.client.request(method,url,**options)
        # 把本次请求耗时记为毫秒，和当前动作一起补方法、路径及真实 HTTP 状态；不把完整认证头/正文写进动作记录。
        # 返回值仍是同一个 HTTPX Response；是否满足业务预期由后续显式断言决定，步骤整体 duration 则用秒。
        self.elapsed=round((time.monotonic()-started)*1000,2)
        if self.active is not None:
            self.active.update(method=method,path=urlparse(url).path,httpStatus=self.response.status_code,responseTimeMs=self.elapsed)
        return self.response
        # Test: Step9 [结束] 下一步：Test: Step10 → 本类 read，看怎样读取响应或提取变量。

    # 从最近一次响应选择 JSON、响应头、HTTP 状态、耗时或正文；source=variable 则读已提取变量。
    # JMESPath/JSONPath 只是读取规则，不属于 HTTP 报文协议本身。
    # Test: Step10 [开始] 从最近响应读取 JSON/响应头/状态/耗时；JMESPath/JSONPath 是取值规则。
    # 职责：从最近响应或已提取变量读实际值，供 extract 或 assert 使用；本函数不判断测试通过与否。
    # 输入：self 是运行上下文；source 选 json/header/status/response_time/variable，其他正文路径读取文本。
    # 输入：expression 是字段/头名/规则，例如 data.token；query 默认 path，也可用 jmespath/jsonpath/正文 regex。
    # 输入：group 默认0表示正则整个匹配，1/2等表示捕获分组；不是 JSON 数组下标。
    # 返回：实际字段值、文本、HTTP状态整数、耗时等；缺失字段可能是 MISSING/None，非法查询可以抛异常。
    # 内存：read 自己不保存新变量；生成的 extract 语句将返回值放进 rt.variables，再检查是否真的取到。
    # 边界：响应取值需要前面的请求；variable 来源不需要最近响应。JMESPath 是查询规则，不是 HTTP 协议字段。
    # source 选响应的哪部分，expression 是字段/规则，query 选取值语法，group 是正则分组。
    # 概念：Response 是 HTTPX 的响应对象，含 status_code、headers、text；json() 是把响应正文解析成 Python 字典/列表，不是又发一次请求。
    # source 选响应哪个部分，query 选 JSON 查询语法：path 的例子是 data.token，JMESPath 的例子是 data.token，JSONPath 的例子是 $.data.token。
    # JMESPath/jsonpath_ng 是第三方 Python 查询库。它们在已返回的 JSON 上取值，不是 HTTP 协议字段，也不要求被测服务支持这些语法。
    # 正则 regex 用于文本匹配，group 选括号捕获组；MISSING 是 TestPilot 自定义的“没找到”标记，简单 path/JSONPath 使用它，JMESPath 无匹配通常返回 None。
    # rt.read 只取值；extract 语句才把它写进 rt.variables，例如 token。require_variable 检查没取到/None 时立即报错，后续 Bearer {{token}} 再用 rt.value 引用。
    # HTTP status_code 是协议状态，JSON 里的 code 是被测项目业务字段；只检查前者等于 200 不能证明业务成功。
    def read(self, source, expression="", query="path", group=0):
        # 先选数据来源：variable 直接读本次提取变量；其他来源必须先有 response，否则当场提示缺少前置请求。
        # JSON、头、协议状态和请求毫秒耗时都从已有响应取，不重新发送请求；取到值后由调用者决定存变量还是断言。
        if source=="variable": return query_value(self.variables, expression, query)
        if self.response is None: raise ValueError("请先发送请求，再提取或断言")
        if source=="json": return query_value(self.response.json(),expression,query)
        if source=="header": return self.response.headers.get(expression,MISSING)
        if source=="status": return self.response.status_code
        if source=="response_time": return self.elapsed
        # 正文还可按正则取匹配或括号分组；无匹配返回 MISSING，其余正文路径返回原文本。
        # 这里不宣告成功失败，存在性/内容断言下一步检查；提取动作另将返回值放进 variables，后续步骤才能引用。
        if query=="regex":
            match=re.search(expression,self.response.text)
            if not match: return MISSING
            return match.group(group)
        return self.response.text
        # Test: Step10 [结束] 下一步：Test: Step11 → src/testpilot/runners/steps.py 的 assertion。
        # 需要变量关联时顺看 require_variable，再回 Test: Step8 的 value 理解后续请求怎样引用它。

    # 辅助块：name 是刚提取的变量名；读取 self.variables[name]，缺失/None立即报错，防止后续请求带空token。
    # 输出：正常返回None，只在当前步骤记录变量名；变量值留在内存，不在此直接写入报告。
    # 功能：确认提取动作确实拿到非空值，在提取处尽早发现失败。
    # 输入：name 为已写入 self.variables 的变量名；self.active 为当前动作记录。
    # 输出/影响：返回 None；缺失标记或 None 抛错，成功时仅在当前记录写变量名，值留在内存。
    def require_variable(self, name):
        # 在提取处立即确认拿到值，避免到下一请求才发现空 token；缺失或 null 直接失败。
        # 成功只在 active 记录变量名，敏感值留在本条用例内存，随后 value 才按引用读取。
        if self.variables[name] is MISSING or self.variables[name] is None:
            raise ValueError("没有提取到变量 "+name)
        if self.active is not None: self.active["variable"]=name

    # 功能：按照设备屏幕大小执行一个方向的 APP 滑动。
    # 输入：direction 为 up/down/left/right；self.driver 是当前用例的 Appium 驱动。
    # 输出/影响：返回 None；向真实 Appium 会话发送 500 毫秒滑动，位移按屏幕短边的 30% 计算。
    def swipe(self,direction):
        size=self.driver.get_window_size()
        x,y=size["width"]//2,size["height"]//2
        delta=int(min(size.values())*.3)
        dx,dy={"up":(0,-delta),"down":(0,delta),"left":(-delta,0),"right":(delta,0)}[direction]
        self.driver.swipe(x,y,x+dx,y+dy,500)


    # 功能：将响应/变量比较转换成 if 分支的布尔条件，而非直接使整条用例断言失败。
    # 输入：source/expression/query/group 选择数据；operator/expected 指定比较；数据来自最近响应或 self.variables。
    # 输出/影响：返回是否匹配的布尔值，并在当前动作内存记录 conditionMatched；读取错误仍向上抛出。
    def condition(self, source, expression, query, operator, expected, group=0):
        from .steps import assertion
        try:
            assertion(self.read(source,expression,query,group),operator,expected)
            matched = True
        except AssertionError:
            matched = False
        if self.active is not None: self.active["conditionMatched"] = matched
        return matched

    # 功能：检查可视化遍历循环的数据规模，防止生成动作无限膨胀。
    # 输入：values 为经变量替换后的列表或元组，应最多 1000 项。
    # 输出/影响：原样返回合法集合供生成循环遍历；不复制、不保存，类型错误或过大抛 ValueError。
    def loop_values(self, values):
        if not isinstance(values,(list,tuple)) or len(values)>1000:
            raise ValueError("循环数据必须为不超过 1000 项的数组")
        return values

    # Web 用 Playwright 条件等待，APP 用 WebDriverWait；条件不满足可进入 else。
    # 有界等待超时与直接死等几秒不同：条件一满足即可继续，不必耗满全部时间。
    # 功能：在有界等待内判断 Web/APP 元素状态，用于可视化条件分支。
    # 输入：target 为 Playwright Locator 或 APP 定位元组；condition 指状态；timeout 为毫秒；expected/value 为预期。
    # 输出/影响：返回布尔值，并在当前动作记录 conditionMatched；向对应 UI 驱动查询状态，超时表示未匹配。
    def element_condition(self, target, condition, timeout, expected=None, value=""):
        expected = True if expected is None else expected
        if self.driver is not None:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.common.exceptions import TimeoutException
            target = self.value(target)
            # 功能：作为 APP WebDriverWait 的轮询条件，读取外层定位和预期状态。
            # 输入：driver 是等待器每次传入的 Appium 驱动；target/condition/expected/value 来自外层 element_condition。
            # 输出/影响：返回是否符合预期的布尔值；反复查询设备元素，不修改页面，不写报告。
            def matches(driver):
                elements = driver.find_elements(*target)
                if condition == "exists": return bool(elements) == expected
                if condition == "hidden": return (not elements or not elements[0].is_displayed()) == expected
                if not elements: return False
                element = elements[0]
                if condition == "visible": return element.is_displayed() == expected
                if condition == "enabled": return element.is_enabled() == expected
                if condition == "checked": return (element.get_attribute("checked")=="true") == expected
                return value in element.text
            try: WebDriverWait(self.driver, timeout/1000).until(matches);matched = True
            except TimeoutException:matched = False
            if self.active is not None: self.active["conditionMatched"] = matched
            return matched
        from playwright.sync_api import TimeoutError, expect
        try:
            if condition == "visible":
                (expect(target).to_be_visible if expected else expect(target).not_to_be_visible)(timeout=timeout)
            elif condition == "hidden":
                (expect(target).not_to_be_visible if expected else expect(target).to_be_visible)(timeout=timeout)
            elif condition == "exists":
                target.wait_for(state="attached" if expected else "detached",timeout=timeout)
            elif condition == "enabled":
                (expect(target).to_be_enabled if expected else expect(target).to_be_disabled)(timeout=timeout)
            elif condition == "checked":
                (expect(target).to_be_checked if expected else expect(target).not_to_be_checked)(timeout=timeout)
            else: expect(target).to_contain_text(value,timeout=timeout)
            matched = True
        except (AssertionError, TimeoutError):matched = False
        if self.active is not None: self.active["conditionMatched"] = matched
        return matched

    # 功能：登记本次测试新页面并将其设为当前操作页面。
    # 输入：name 为可视化步骤指定的唯一标签名；page 是当前测试浏览器中的 Playwright Page。
    # 输出/影响：返回 page；更新 self.tabs/self.page 并 bring_to_front，重复名称时报错，不操作用户日常 Chrome。
    def register_tab(self, name, page):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        self.tabs[name] = page
        self.page = page
        page.bring_to_front()
        return page

    # 功能：在当前测试浏览器上下文中新建并登记一个命名页。
    # 输入：name 为新页面唯一名称；self.page.context 提供本次测试的浏览器上下文。
    # 输出/影响：返回新 Page；创建真实标签页并更新内存页表，重名时不创建。
    def open_tab(self, name):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        return self.register_tab(name,self.page.context.new_page())

    # 功能：把后续动作切换到已登记且未关闭的测试标签页。
    # 输入：name 为 self.tabs 中的页面名，例如 main；self 为本次用例上下文。
    # 输出/影响：返回对应 Page；更新 self.page 并切到前台，不存在或已关闭时报错。
    def switch_tab(self, name):
        if name not in self.tabs or self.tabs[name].is_closed(): raise ValueError("标签页未打开："+name)
        self.page = self.tabs[name];self.page.bring_to_front();return self.page

    # 只关闭此测试自己打开的命名页，保留 main；这里控制的是测试浏览器，不是用户日常 Chrome。
    # 功能：关闭本次测试新增的命名页面，然后回到保留的 main 页面。
    # 输入：name 为 self.tabs 中的新增页面名，禁止传 main。
    # 输出/影响：返回 main Page；从内存页表删除并关闭指定测试标签，不涉及用户已有浏览器标签。
    def close_tab(self, name):
        if name == "main": raise ValueError("保留主标签页 main；仅关闭新增标签页")
        if name not in self.tabs: raise ValueError("标签页未打开："+name)
        self.tabs.pop(name).close()
        return self.switch_tab("main")

    # 功能：点击元素并等待弹出的新页面，再登记为后续操作页。
    # 输入：locator 为要点击的 Playwright Locator；name 为新页名；timeout 为等待及点击的毫秒上限。
    # 输出/影响：返回新 Page；真实点击并更新 self.tabs/self.page，无弹出或超时由 Playwright 报错。
    def click_popup(self, locator, name, timeout):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        with self.page.expect_popup(timeout=timeout) as popup:
            locator.click(timeout=timeout)
        return self.register_tab(name,popup.value)

    # 功能：点击下载控件，等待下载并保存为本次运行的证据文件。
    # 输入：locator 为下载触发控件；filename 为简单文件名；timeout 为毫秒上限。
    # 输出/影响：返回 None；在 report_path 同目录写下载文件，非法路径或下载超时会报错。
    def download(self, locator, filename, timeout):
        if Path(filename).name != filename or "\\" in filename or filename in {".",".."}: raise ValueError("下载使用简单文件名")
        with self.page.expect_download(timeout=timeout) as download:
            locator.click(timeout=timeout)
        download.value.save_as(str(Path(self.report_path).parent/filename))

    # 功能：把常见按键名或数值转换成 Android keycode 并发送给设备。
    # 输入：value 为 BACK/ENTER/HOME/DELETE/TAB 或 0 到 300 的按键码；driver 来自 APP fixture。
    # 输出/影响：返回 None；通过 Appium 执行按键，无法解析或超范围会报错。
    def press_key(self, value):
        keys={"BACK":4,"ENTER":66,"HOME":3,"DELETE":67,"TAB":61}
        code=keys.get(str(value).upper())
        if code is None:
            try:code=int(value)
            except (ValueError,TypeError):raise ValueError("APP 按键使用 BACK/ENTER/HOME/DELETE/TAB 或 Android keycode") from None
        if not 0<=code<=300: raise ValueError("Android keycode 超出范围")
        self.driver.press_keycode(code)
