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
def substitute(value, variables):
    if isinstance(value,dict):return {k:substitute(v,variables) for k,v in value.items()}
    if isinstance(value,list):return [substitute(v,variables) for v in value]
    if not isinstance(value,str):return value
    def resolve(key):
        if key.startswith("env."):
            name=key[4:]
            if name not in {"TEST_USERNAME","TEST_PASSWORD","TEST_TOKEN"}:raise ValueError("只能引用 TEST_USERNAME/TEST_PASSWORD/TEST_TOKEN")
            if name not in os.environ:raise ValueError(f"未配置测试凭据变量 {name}")
            return os.environ[name]
        if key in variables:return variables[key]
        try:return json_value(variables,key)
        except (KeyError,IndexError,TypeError,ValueError):raise ValueError(f"变量尚未提取或字段不存在：{key}") from None
    matched=re.fullmatch(r"\{\{\s*([\w.\[\]-]+)\s*\}\}",value)
    if matched:return resolve(matched[1])
    return re.sub(r"\{\{\s*([\w.\[\]-]+)\s*\}\}",lambda m:str(resolve(m[1])),value)

def json_value(value,path):
    path=path.removeprefix("$").lstrip(".")
    if not path:return value
    if not re.fullmatch(r"[\w.\[\]0-9-]+",path):raise ValueError("JSON 路径支持字段和数组下标，例如 data.users[0].id")
    for token in re.findall(r"([^\.\[\]]+)|\[(-?\d+)\]",path):
        key,index=token
        value=value[int(index)] if index else value[key]
    return value

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
def assertion(actual,operator,expected):
    from .visual_runtime import MISSING
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

def web_locator(page,step):
    by=step["by"];selector=step["selector"]
    if by=="role":return page.get_by_role(step["role"],name=selector,exact=True)
    if by=="label":return page.get_by_label(selector,exact=True)
    if by=="text":return page.get_by_text(selector,exact=True)
    if by=="testid":return page.get_by_test_id(selector)
    return page.locator(("xpath=" if by=="xpath" else "")+selector)

# 历史版本兼容入口，直接遍历步骤执行；新生成代码不再把所有步骤藏进这个函数调用里。
def execute_steps(kind,steps,base_url,page=None,driver=None):
    variables={};response=None;elapsed=0;records=[]
    def sanitize(value):
        text=json.dumps(value,ensure_ascii=False,default=str)
        for secret in [str(v) for k,v in variables.items() if any(w in k.lower() for w in ["token","password","secret"]) and v]:
            text=text.replace(secret,"[REDACTED]")
        for name in ["TEST_PASSWORD","TEST_TOKEN"]:
            secret=os.environ.get(name)
            if secret:text=text.replace(secret,"[REDACTED]")
        # Extracted values stay in memory; never include token/header/body values in step reports.
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

# Kept execute_steps for immutable historical revisions; new assets use readable blocks.
from .visual_runtime import StepRuntime
