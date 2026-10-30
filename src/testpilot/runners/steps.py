# 学习导航：Test: Step11 只读 assertion；旧 execute_steps 入口不是当前可视化编译主线。
# 基础变量替换和断言助手，同时保留历史 execute_steps 解释执行器。
# 当前新版本由 steps.py 编译成可读代码并使用 StepRuntime；不要沿旧 execute_steps 理解全部新功能。

"""Runtime for visual steps. No database or model access inside test containers."""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin
import httpx

# 处理字典、数组和字符串中的变量引用；{{env.*}} 只允许读取指定测试凭据变量。
# 后续请求头可写 Authorization: Bearer {{token}}，运行到该步骤才取前面提取的值。
# 辅助块：value 是待替换的结构，variables 是本次用例提取变量字典；调用者是 rt.value 等运行时方法。
# 返回：替换后的同类结构/值；不存在的引用报错；只在内存中处理，env.*仅允许列出的测试凭据环境变量。
# 功能：解析可视化步骤中的 {{变量}}，支持嵌套字典和数组；纯变量引用保留原类型。
# 输入：value 是待处理的步骤参数；variables 是当前用例先前提取的变量字典。
# 输出/影响：返回替换后的结构；读取允许的测试凭据环境变量，不写文件；引用缺失会报错。
def substitute(value, variables):
    # 参数可能是请求头、JSON 对象或数组，因此按结构递归替换；数字、布尔值等不含模板的值保留原类型。
    # resolve 只从本次用例变量或允许的测试凭据取实际值，字段不存在就报错，不把空内容悄悄带进请求。
    if isinstance(value,dict):return {k:substitute(v,variables) for k,v in value.items()}
    if isinstance(value,list):return [substitute(v,variables) for v in value]
    if not isinstance(value,str):return value
    # 功能：替外层 substitute 解析一个变量名称，是共享 variables 的嵌套辅助函数。
    # 输入：key 是模板括号里的名称，如 token、user.id、env.TEST_TOKEN；读取外层变量字典。
    # 输出/影响：返回实际变量值；仅允许三种测试凭据环境变量，未配置或字段缺失时抛 ValueError。
    def resolve(key):
        if key.startswith("env."):
            name=key[4:]
            if name not in {"TEST_USERNAME","TEST_PASSWORD","TEST_TOKEN"}:raise ValueError("只能引用 TEST_USERNAME/TEST_PASSWORD/TEST_TOKEN")
            if name not in os.environ:raise ValueError(f"未配置测试凭据变量 {name}")
            return os.environ[name]
        if key in variables:return variables[key]
        try:return json_value(variables,key)
        except (KeyError,IndexError,TypeError,ValueError):raise ValueError(f"变量尚未提取或字段不存在：{key}") from None
    # 模板占满整段字符串时直接返回原值，如 {{count}} 仍是数字；Bearer {{token}} 这种混合文本才拼成字符串。
    # 这里只处理内存参数，后面的 HTTPX/UI 动作拿到替换结果后才真正执行。
    matched=re.fullmatch(r"\{\{\s*([\w.\[\]-]+)\s*\}\}",value)
    if matched:return resolve(matched[1])
    return re.sub(r"\{\{\s*([\w.\[\]-]+)\s*\}\}",lambda m:str(resolve(m[1])),value)

# 功能：按简单字段路径从 JSON 结构中取值，不是完整 JSONPath 查询引擎。
# 输入：value 是字典/数组等结构；path 如 data.users[0].id，允许可选的 $ 前缀。
# 输出/影响：返回路径对应的值；不改变原数据，非法表达式或缺失字段由异常说明。
def json_value(value,path):
    path=path.removeprefix("$").lstrip(".")
    if not path:return value
    if not re.fullmatch(r"[\w.\[\]0-9-]+",path):raise ValueError("JSON 路径支持字段和数组下标，例如 data.users[0].id")
    for token in re.findall(r"([^\.\[\]]+)|\[(-?\d+)\]",path):
        key,index=token
        value=value[int(index)] if index else value[key]
    return value

# 功能：给历史解释执行器选择断言或提取所需的响应信息。
# 输入：step 含 source/expression；response 是最近 HTTPX 响应；variables 是运行变量；elapsed 为请求毫秒耗时。
# 输出/影响：返回字段、头、状态码、耗时或文本；不保存变量，不写文件；缺少前置请求时报错。
def select_response(step,response,variables,elapsed):
    if step["source"]=="variable":return json_value(variables,step["expression"])
    if response is None:raise ValueError("请先发送请求，再提取或断言响应")
    return {
        "json":lambda:json_value(response.json(),step["expression"]),
        "header":lambda:response.headers[step["expression"]],
        "status":lambda:response.status_code,
        "response_time":lambda:elapsed,
        "text":lambda:response.text,
    }[step["source"]]()

# 统一比较操作的含义：eq 相等、contains 包含、exists 存在等。
# 不满足抛 AssertionError，pytest 据此记录断言失败；打印一句“失败”不会产生同样效果。
# Test: Step11 [开始] 比较实际值 actual 和期望 expected；条件不满足就抛出 AssertionError。
# 概念：断言是把真实结果与明确预期比较，例如 HTTP 状态 eq 200、字段 contains 某文字、值 exists；不是请模型猜测结果是否成功。
# assertion 是 TestPilot 的比较函数，AssertionError 则是 Python 的异常类型。抛异常会中断当前正常动作，rt.step 保存失败位置，pytest 再把测试项记为失败。
# 这里的 actual 来自前面的响应读取，expected 来自用例配置并可做变量替换；生成用例时必须给出预期，发出请求本身不算验证。
# 职责：按照明确的比较规则检查实际值；由生成脚本/运行时调用，不让模型凭感觉决定是否通过。
# 输入：actual 是 read 取到的值；operator 如 eq/contains/exists/gt；expected 是经变量替换后的期望值。
# 返回：满足条件时不返回业务值（None）；不满足抛 AssertionError，不返回 {success:false}。
# 数据：比较只在测试进程内存里完成，不自行写数据库或文件；异常由 StepRuntime 和 pytest 记录。
# 边界：字段缺失和 null 参与存在性判断；“HTTP200”与“业务字段符合预期”必须分别配置检查。
# actual 是实际取到的值，operator 是比较方式，expected 是用户要求的预期。
def assertion(actual,operator,expected):
    from .visual_runtime import MISSING
    # 按用户配置比较实际值：存在性专门处理未找到/null；其余规则检查相等、包含、正则、长度、类型或大小。
    # expected 来自可视化步骤并已替换变量，actual 来自响应读取；类型检查刻意不把 Python 布尔值当 JSON 数字。
    # 这段只得出内存里的 ok；不满足就向下抛真实 AssertionError，由 rt.step 记录失败位置、pytest 汇总失败。
    if operator=="exists":ok=actual is not MISSING and actual is not None
    elif operator=="not_exists":ok=actual is MISSING or actual is None
    elif actual is MISSING:ok=False
    elif operator=="eq":ok=actual==expected
    elif operator=="ne":ok=actual!=expected
    elif operator=="contains":ok=expected in actual
    elif operator=="matches":ok=re.search(str(expected),str(actual)) is not None
    elif operator=="length":ok=len(actual)==int(expected)
    elif operator=="type":
        expected_types={"string":str,"number":(int,float),"boolean":bool,"object":dict,"array":list,"null":type(None)}
        ok=expected in expected_types and isinstance(actual,expected_types[expected]) and not (expected=="number" and isinstance(actual,bool))
    else:ok={"gt":lambda:actual>expected,"gte":lambda:actual>=expected,"lt":lambda:actual<expected,"lte":lambda:actual<=expected}[operator]()
    if not ok:raise AssertionError(f"实际值 {actual!r}，期望 {operator} {expected!r}")
    # Test: Step11 [结束] 下一步：Test: Step12 → src/testpilot/runners/visual_runtime.py 的 StepRuntime.step。
    # 失败时按异常路径读，成功时按 yield 后的 passed 路径读，两种结果最后都要 flush。

# 功能：将历史可视化步骤里的定位设置转换成 Playwright Locator。
# 输入：page 是本次测试页面；step 含 by、selector，role 定位时另含 role。
# 输出/影响：返回尚未执行点击等动作的 Locator；不写数据库，真正操作由调用者执行。
def web_locator(page,step):
    by=step["by"];selector=step["selector"]
    if by=="role":return page.get_by_role(step["role"],name=selector,exact=True)
    if by=="label":return page.get_by_label(selector,exact=True)
    if by=="text":return page.get_by_text(selector,exact=True)
    if by=="testid":return page.get_by_test_id(selector)
    return page.locator(("xpath=" if by=="xpath" else "")+selector)

# 历史版本兼容入口，直接遍历步骤执行；新生成代码不再把所有步骤藏进这个函数调用里。
# 功能：解释执行旧版 API/Web/APP 步骤，兼容历史用例；新版本优先使用编译后的原生代码。
# 输入：kind 区分 api/web/app；steps 是步骤列表；base_url 是测试环境地址；page/driver 为对应 UI 驱动。
# 输出/影响：无业务返回值；请求/界面动作真实执行，变量只在本次调用内存中，逐步写 /work/steps.json；失败抛 AssertionError。
def execute_steps(kind,steps,base_url,page=None,driver=None):
    variables={};response=None;elapsed=0;records=[]
    # 功能：替旧执行器的错误与步骤记录隐藏已知敏感变量，读取外层 variables。
    # 输入：value 为待输出数据；敏感来源是 token/password/secret 命名的变量及测试凭据环境变量。
    # 输出/影响：返回 JSON 化后的替换结果，不改原变量或写文件；这是有限规则，不能识别所有秘密。
    def sanitize(value):
        text=json.dumps(value,ensure_ascii=False,default=str)
        for secret in [str(v) for k,v in variables.items() if any(w in k.lower() for w in ["token","password","secret"]) and v]:
            text=text.replace(secret,"[REDACTED]")
        for name in ["TEST_PASSWORD","TEST_TOKEN"]:
            secret=os.environ.get(name)
            if secret:text=text.replace(secret,"[REDACTED]")
        # 提取值放在本条用例的 variables 内存字典，步骤记录通常只写变量名，不写整份请求头/正文；sanitize 另外替换已知测试凭据及 token/password/secret 名称变量的值，不能保证识别所有敏感内容。
        return json.loads(text)
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        assert any(s["enabled"] for s in steps),"没有启用的步骤，未验证任何行为"
        for index,original in enumerate(steps):
            record={"index":index+1,"id":original["id"],"name":original["name"],"action":original["action"],"status":"skipped"}
            records.append(record)
            if not original["enabled"]:continue
            started=time.monotonic()
            try:
                s=substitute(original,variables);action=s["action"]
                if kind=="api":
                    if action=="request":
                        target=urljoin(base_url.rstrip("/")+"/",s["path"].lstrip("/"))
                        opts={"headers":s["headers"],"params":s["params"],"timeout":s["timeout"]/1000}
                        if s["body"] is not None:opts[{"json":"json","form":"data","text":"content"}[s["body_type"]]]=s["body"]
                        request_started=time.monotonic();response=client.request(s["method"],target,**opts);elapsed=round((time.monotonic()-request_started)*1000,2)
                        record.update(method=s["method"],path=s["path"],httpStatus=response.status_code,responseTimeMs=elapsed)
                        if s.get("expected_status") is not None:
                            record["expectedStatus"]=s["expected_status"]
                            assertion(response.status_code,"eq",s["expected_status"])
                        if s.get("body_pattern"):assertion(response.text,"matches",s["body_pattern"])
                    elif action=="extract":
                        variables[s["variable"]]=select_response(s,response,variables,elapsed)
                        record["variable"]=s["variable"]
                    else:
                        actual=select_response(s,response,variables,elapsed)
                        assertion(actual,s["operator"],s["expected"])
                        record.update(source=s["source"],expression=s["expression"],operator=s["operator"])
                elif kind=="web":
                    from playwright.sync_api import expect
                    if action=="goto":page.goto(urljoin(base_url.rstrip("/")+"/",s["path"].lstrip("/")),timeout=s["timeout"])
                    elif action=="assert_url":expect(page).to_have_url(re.compile(s["value"]),timeout=s["timeout"])
                    else:
                        locator=web_locator(page,s)
                        if action=="click":locator.click(timeout=s["timeout"])
                        elif action=="fill":locator.fill(s["value"],timeout=s["timeout"])
                        elif action=="press":locator.press(s["value"],timeout=s["timeout"])
                        elif action=="select":locator.select_option(s["value"],timeout=s["timeout"])
                        elif action=="check":locator.check(timeout=s["timeout"])
                        elif action in {"wait","assert_visible"}:expect(locator).to_be_visible(timeout=s["timeout"])
                        elif action=="assert_text":expect(locator).to_contain_text(s["value"],timeout=s["timeout"])
                        elif action=="assert_enabled":expect(locator).to_be_enabled(timeout=s["timeout"]) if s["expected"] is not False else expect(locator).to_be_disabled(timeout=s["timeout"])
                else:
                    from selenium.webdriver.support.ui import WebDriverWait
                    from selenium.webdriver.support import expected_conditions as EC
                    if action=="back":driver.back()
                    elif action=="swipe":
                        size=driver.get_window_size();x,y=size["width"]//2,size["height"]//2
                        delta=int(min(size.values())*.3);dx,dy={"up":(0,-delta),"down":(0,delta),"left":(-delta,0),"right":(delta,0)}[s["direction"]]
                        driver.swipe(x,y,x+dx,y+dy,500)
                    else:
                        loc=("-android uiautomator" if s["by"]=="android uiautomator" else s["by"],s["selector"]);wait=WebDriverWait(driver,s["timeout"]/1000)
                        element=wait.until(EC.element_to_be_clickable(loc) if action in {"click","fill"} else EC.visibility_of_element_located(loc))
                        if action=="click":element.click()
                        elif action=="fill":element.clear();element.send_keys(s["value"])
                        elif action=="assert_text":wait.until(lambda d:s["value"] in d.find_element(*loc).text)
                        elif action=="assert_enabled":wait.until(lambda d:d.find_element(*loc).is_enabled()==(s["expected"] is not False))
                record["status"]="passed"
            except Exception as error:
                record.update(status="failed",error=str(error)[:4000])
                raise AssertionError(sanitize(f"第 {index+1} 步「{original['name']}」失败：{error}")) from None
            finally:
                record["duration"]=round(time.monotonic()-started,3)
                Path("/work/steps.json").write_text(json.dumps(sanitize(records),ensure_ascii=False))

# 保留 execute_steps 供早期保存、不能随意改写的历史用例版本调用；新编译用例生成可读的 rt.step 动作块，正式执行入口仍是 pytest，不在此重新实现一套 Runner。
from .visual_runtime import StepRuntime
