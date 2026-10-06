# 生成脚本运行时的助手：请求、变量、步骤记录和部分 UI 辅助操作。
# StepRuntime 是 AIQA 自定义类，不是 Python/pytest 内置；此模块运行在测试容器中，不连接平台数据库。

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
    def __repr__(self): return "<字段缺失>"
MISSING = Missing()

# 在已经拿到的 JSON 上查询字段：path 是简单路径，JMESPath/JSONPath 是第三方查询语言。
# 字段缺失使用 MISSING 哨兵，便于区分“根本没有这个字段”和正常返回的值。
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
class StepRuntime:
    def __init__(self, base_url, page=None, driver=None, report_path="/work/steps.json"):
        self.base_url, self.page, self.driver = base_url, page, driver
        self.variables, self.records = {}, []
        self.response, self.elapsed, self.active = None, 0, None
        self.report_path = report_path
        self.tabs = {"main": page} if page else {}
        self.operations = 0

    def __enter__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=False)
        return self

    def __exit__(self, *args):
        self.client.close()
        self.flush()

    # 递归替换 {{token}} 等引用；完整变量占满字符串时保留原类型，字符串拼接时才转成文本。
    def value(self, value):
        from .steps import substitute
        if isinstance(value, tuple): return tuple(self.value(v) for v in value)
        return substitute(value, self.variables)

    # 只接受当前环境下的相对路径，拒绝变量把目标换成任意绝对 URL。
    # 这是可视化请求的目标检查，不能当成对任意原生 Python 脚本的网络防火墙。
    def url(self, path):
        if not isinstance(path,str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("路径必须是当前环境内的相对路径")
        # Keep the leading slash: stripping it before urljoin would turn the
        # legitimate path /http://other.test into an absolute external URL.
        return self.base_url.rstrip("/") + path

    def file(self, name):
        path = Path(name)
        if path.name != name or "\\" in name or name in {".",".."}: raise ValueError("上传文件使用简单文件名")
        if not path.is_file(): raise ValueError("请在辅助文件中添加 "+name)
        return str(path.resolve())

    # 记录步骤时替换已知测试凭据和名称像 token/password 的变量值。
    # 这是有限规则，不保证任意嵌套内容、截图或所有网络正文都已无敏感信息。
    def sanitize(self, value):
        secrets = [str(v) for k,v in self.variables.items() if any(w in k.lower() for w in ["token","password","secret"]) and v]
        secrets += [os.environ.get(n,"") for n in ["TEST_PASSWORD","TEST_TOKEN"]]
        def clean(item):
            if isinstance(item,dict):return {k:clean(v) for k,v in item.items()}
            if isinstance(item,list):return [clean(v) for v in item]
            if isinstance(item,str):
                for secret in sorted(set(secrets),key=len,reverse=True):
                    if secret:item=item.replace(secret,"[REDACTED]")
            return item
        return clean(value)

    def flush(self):
        Path(self.report_path).write_text(json.dumps(self.sanitize(self.records), ensure_ascii=False))

    def skip(self, info):
        self.operations += 1
        if self.operations > 5000: raise ValueError("单次用例最多执行或跳过 5000 个步骤")
        self.records.append(dict(info,status="skipped",duration=0))
        self.flush()

    # contextmanager 包住真实动作：进入记录 running，正常退出 passed，异常记录失败并继续抛出。
    # finally 不论成功失败都记录耗时并落盘，所以错误步骤也能在报告中定位。
    # 循环可能多次执行同一个步骤 ID，execution 表示本次执行序号，不能只按静态 ID 覆盖结果。
    @contextmanager
    def step(self, info):
        self.operations += 1
        if self.operations > 5000: raise ValueError("单次用例最多执行 5000 个步骤，检查循环次数")
        record = dict(info, status="running", execution=len(self.records)+1)
        self.records.append(record)
        self.active = record
        started=time.monotonic()
        self.flush()
        try:
            yield
            record["status"]="passed"
        except pytest.skip.Exception as error:
            record.update(status="skipped",error=str(error))
            raise
        except Exception as error:
            record.update(status="failed",error=str(error)[:4000])
            raise AssertionError(self.sanitize(f"第 {info['index']} 步「{info['name']}」失败：{error}")) from None
        finally:
            record["duration"]=round(time.monotonic()-started,3)
            self.flush()
            self.active=None

    # HTTPX Client 真正发出 HTTP 请求；复用连接和 Cookie，并记录状态码与耗时。
    # 不在此猜业务是否成功；HTTP/JSON/业务效果的检查由后续断言明确表达。
    def request(self, method, url, **options):
        started=time.monotonic()
        uploads = options.pop("uploads", {})
        with ExitStack() as opened:
            if uploads:
                options["files"] = {field:(filename,opened.enter_context(open(self.file(filename),"rb"))) for field,filename in uploads.items()}
            self.response=self.client.request(method,url,**options)
        self.elapsed=round((time.monotonic()-started)*1000,2)
        if self.active is not None:
            self.active.update(method=method,path=urlparse(url).path,httpStatus=self.response.status_code,responseTimeMs=self.elapsed)
        return self.response

    # 从最近一次响应选择 JSON、响应头、HTTP 状态、耗时或正文；source=variable 则读已提取变量。
    # JMESPath/JSONPath 只是读取规则，不属于 HTTP 报文协议本身。
    def read(self, source, expression="", query="path", group=0):
        if source=="variable": return query_value(self.variables, expression, query)
        if self.response is None: raise ValueError("请先发送请求，再提取或断言")
        if source=="json": return query_value(self.response.json(),expression,query)
        if source=="header": return self.response.headers.get(expression,MISSING)
        if source=="status": return self.response.status_code
        if source=="response_time": return self.elapsed
        if query=="regex":
            match=re.search(expression,self.response.text)
            if not match: return MISSING
            return match.group(group)
        return self.response.text

    def require_variable(self, name):
        if self.variables[name] is MISSING or self.variables[name] is None:
            raise ValueError("没有提取到变量 "+name)
        if self.active is not None: self.active["variable"]=name

    def swipe(self,direction):
        size=self.driver.get_window_size()
        x,y=size["width"]//2,size["height"]//2
        delta=int(min(size.values())*.3)
        dx,dy={"up":(0,-delta),"down":(0,delta),"left":(-delta,0),"right":(delta,0)}[direction]
        self.driver.swipe(x,y,x+dx,y+dy,500)


    def condition(self, source, expression, query, operator, expected, group=0):
        from .steps import assertion
        try:
            assertion(self.read(source,expression,query,group),operator,expected)
            matched = True
        except AssertionError:
            matched = False
        if self.active is not None: self.active["conditionMatched"] = matched
        return matched

    def loop_values(self, values):
        if not isinstance(values,(list,tuple)) or len(values)>1000:
            raise ValueError("循环数据必须为不超过 1000 项的数组")
        return values

    # Web 用 Playwright 条件等待，APP 用 WebDriverWait；条件不满足可进入 else。
    # 有界等待超时与直接死等几秒不同：条件一满足即可继续，不必耗满全部时间。
    def element_condition(self, target, condition, timeout, expected=None, value=""):
        expected = True if expected is None else expected
        if self.driver is not None:
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.common.exceptions import TimeoutException
            target = self.value(target)
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

    def register_tab(self, name, page):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        self.tabs[name] = page
        self.page = page
        page.bring_to_front()
        return page

    def open_tab(self, name):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        return self.register_tab(name,self.page.context.new_page())

    def switch_tab(self, name):
        if name not in self.tabs or self.tabs[name].is_closed(): raise ValueError("标签页未打开："+name)
        self.page = self.tabs[name];self.page.bring_to_front();return self.page

    # 只关闭此测试自己打开的命名页，保留 main；这里控制的是测试浏览器，不是用户日常 Chrome。
    def close_tab(self, name):
        if name == "main": raise ValueError("保留主标签页 main；仅关闭新增标签页")
        if name not in self.tabs: raise ValueError("标签页未打开："+name)
        self.tabs.pop(name).close()
        return self.switch_tab("main")

    def click_popup(self, locator, name, timeout):
        if name in self.tabs: raise ValueError("标签页名称已存在："+name)
        with self.page.expect_popup(timeout=timeout) as popup:
            locator.click(timeout=timeout)
        return self.register_tab(name,popup.value)

    def download(self, locator, filename, timeout):
        if Path(filename).name != filename or "\\" in filename or filename in {".",".."}: raise ValueError("下载使用简单文件名")
        with self.page.expect_download(timeout=timeout) as download:
            locator.click(timeout=timeout)
        download.value.save_as(str(Path(self.report_path).parent/filename))

    def press_key(self, value):
        keys={"BACK":4,"ENTER":66,"HOME":3,"DELETE":67,"TAB":61}
        code=keys.get(str(value).upper())
        if code is None:
            try:code=int(value)
            except (ValueError,TypeError):raise ValueError("APP 按键使用 BACK/ENTER/HOME/DELETE/TAB 或 Android keycode") from None
        if not 0<=code<=300: raise ValueError("Android keycode 超出范围")
        self.driver.press_keycode(code)
