# JMeter 原生测试计划和结果处理：Python 负责生成/改写 XML 与分析 CSV，负载由 JMeter 发出。
# JMX 是计划，辅助 CSV 是测试数据，JTL 是执行后采样结果，三者用途不同。

"""Native JMeter assets and report semantics, independent of the language model."""
import csv
import math
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any
from urllib.parse import urlencode
from pydantic import BaseModel, Field, model_validator

class Stage(BaseModel):
    users: int = Field(ge=1, le=100)
    seconds: int = Field(ge=1, le=1800)
    ramp_seconds: int = Field(default=1, ge=0, le=300)

# 运行配置保存用户数、时长、预热和判定阈值；这些是测试条件，不是模型提示词。
class PerformanceConfig(BaseModel):
    users: int = Field(default=5, ge=1, le=100)
    duration_seconds: int = Field(default=30, ge=1, le=1800)
    ramp_seconds: int = Field(default=3, ge=0, le=300)
    think_ms: int = Field(default=200, ge=0, le=10000)
    warmup_seconds: int = Field(default=0, ge=0, le=600)
    p95_ms: float | None = Field(default=500, gt=0)
    max_error_rate: float | None = Field(default=.01, ge=0, le=1)
    min_requests: int = Field(default=1, ge=1, le=1000000)
    stages: list[Stage] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def limits(self):
        duration = sum(s.seconds for s in self.stages) if self.stages else self.duration_seconds
        if duration > 1800 or self.warmup_seconds >= duration:
            raise ValueError("压测总时长最多 1800 秒，预热必须小于总时长")
        if any(s.ramp_seconds > s.seconds for s in self.stages) or (not self.stages and self.ramp_seconds > duration):
            raise ValueError("增长时间不能超过阶段时长")
        return self

class RequestSpec(BaseModel):
    id: str = ""
    name: str = Field(min_length=1, max_length=120)
    method: str = Field(default="GET", pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    path: str = Field(min_length=1, max_length=2000)
    body: Any = None
    body_type: str = Field(default="json", pattern="^(json|form|text)$")
    params: dict[str, Any] = Field(default_factory=dict)
    timeout: int | None = Field(default=None, ge=100, le=60000)
    follow_redirects: bool | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    expected_status: int = Field(default=200, ge=100, le=599)
    body_pattern: str = Field(default="", max_length=1000)
    extract_json: dict[str, str] = Field(default_factory=dict)

def prop(parent, name, value, tag="stringProp"):
    return _prop(parent, name, value, tag)

def _prop(parent, name, value, tag):
    node = ET.SubElement(parent, tag, name=name)
    node.text = str(value).lower() if isinstance(value, bool) else str(value)
    return node

def component(parent, tag, name, gui=""):
    node = ET.SubElement(parent, tag, guiclass=gui or tag+"Gui", testclass=tag, testname=name, enabled="true")
    tree = ET.SubElement(parent, "hashTree")
    return node, tree

# 生成含标准线程组、HTTP 请求、提取器与断言的 JMX；保存计划时不会产生测试流量。
def build_plan(requests):
    specs = [RequestSpec.model_validate(r) for r in requests]
    if not specs or len(specs) > 30:
        raise ValueError("需要 1～30 个 HTTP 请求")
    root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.6.3")
    top = ET.SubElement(root, "hashTree")
    plan, tree = component(top, "TestPlan", "AIQA 性能测试", "TestPlanGui")
    prop(plan, "TestPlan.functional_mode", False, "boolProp")
    prop(plan, "TestPlan.serialize_threadgroups", False, "boolProp")
    args = ET.SubElement(plan, "elementProp", name="TestPlan.user_defined_variables", elementType="Arguments", guiclass="ArgumentsPanel", testclass="Arguments")
    ET.SubElement(args, "collectionProp", name="Arguments.arguments")
    group, group_tree = component(tree, "ThreadGroup", "负载阶段", "ThreadGroupGui")
    prop(group, "ThreadGroup.on_sample_error", "continue")
    loops = ET.SubElement(group, "elementProp", name="ThreadGroup.main_controller", elementType="LoopController", guiclass="LoopControlPanel", testclass="LoopController", enabled="true")
    prop(loops, "LoopController.continue_forever", False, "boolProp")
    prop(loops, "LoopController.loops", -1)
    for name, value in [("num_threads","${__P(users,5)}"),("ramp_time","${__P(ramp,3)}"),("duration","${__P(duration,30)}"),("delay",0)]:
        prop(group, "ThreadGroup."+name, value)
    prop(group, "ThreadGroup.scheduler", True, "boolProp")
    cookie, _ = component(group_tree, "CookieManager", "每个用户独立 Cookie", "CookiePanel")
    ET.SubElement(cookie, "collectionProp", name="CookieManager.cookies")
    prop(cookie, "CookieManager.clearEachIteration", False, "boolProp")
    timer, _ = component(group_tree, "ConstantTimer", "操作间隔", "ConstantTimerGui")
    prop(timer, "ConstantTimer.delay", "${__P(think_ms,200)}")
    for spec in specs:
        if spec.id:
            group_tree.append(ET.Comment(" AIQA step: "+spec.id.encode().hex()+" "))
        sampler, child = component(group_tree, "HTTPSamplerProxy", spec.name, "HttpTestSampleGui")
        args = ET.SubElement(sampler, "elementProp", name="HTTPsampler.Arguments", elementType="Arguments", guiclass="HTTPArgumentsPanel", testclass="Arguments")
        collection = ET.SubElement(args, "collectionProp", name="Arguments.arguments")
        if spec.body is not None and spec.body != "":
            if spec.body_type=="form":
                if not isinstance(spec.body,dict):raise ValueError("表单请求体需要键值对象")
                for key,value in spec.body.items():
                    arg=ET.SubElement(collection,"elementProp",name=key,elementType="HTTPArgument")
                    prop(arg,"HTTPArgument.always_encode",True,"boolProp")
                    prop(arg,"Argument.name",key);prop(arg,"Argument.value",value)
                    prop(arg,"Argument.metadata","=")
            else:
                import json
                body=spec.body if isinstance(spec.body,str) else json.dumps(spec.body,ensure_ascii=False)
                if len(body.encode())>20000:raise ValueError("性能请求体最多 20KB")
                arg = ET.SubElement(collection, "elementProp", name="", elementType="HTTPArgument")
                prop(arg, "HTTPArgument.always_encode", False, "boolProp")
                prop(arg, "Argument.value", body)
                prop(arg, "Argument.metadata", "=")
                prop(sampler, "HTTPSampler.postBodyRaw", True, "boolProp")
        path=spec.path
        if not path.startswith("/") or path.startswith("//"):raise ValueError("性能请求路径必须相对于当前环境")
        if spec.params:path+=("&" if "?" in path else "?")+urlencode(spec.params,doseq=True,safe="${}")
        for name, value in [("domain","${__P(target_host)}"),("port","${__P(target_port)}"),("protocol","${__P(target_protocol,http)}"),("path",path),("method",spec.method),("connect_timeout",spec.timeout or 5000),("response_timeout",spec.timeout or 10000)]:
            prop(sampler, "HTTPSampler."+name, value)
        prop(sampler,"HTTPSampler.follow_redirects",True if spec.follow_redirects is None else spec.follow_redirects,"boolProp")
        prop(sampler,"HTTPSampler.use_keepalive",True,"boolProp")
        headers = dict(spec.headers)
        if spec.body is not None and spec.body != "" and not any(k.lower()=="content-type" for k in headers):
            headers["Content-Type"] = {"json":"application/json","form":"application/x-www-form-urlencoded","text":"text/plain"}[spec.body_type]
        if headers:
            manager, _ = component(child, "HeaderManager", "请求头", "HeaderPanel")
            collection = ET.SubElement(manager, "collectionProp", name="HeaderManager.headers")
            for key, value in headers.items():
                h = ET.SubElement(collection, "elementProp", name="", elementType="Header")
                prop(h, "Header.name", key); prop(h, "Header.value", value)
        for field, expected, match in [("Assertion.response_code",str(spec.expected_status),8),("Assertion.response_data",spec.body_pattern,2)]:
            if not expected: continue
            assertion, _ = component(child, "ResponseAssertion", "检查 "+field, "AssertionGui")
            collection = ET.SubElement(assertion, "collectionProp", name="Asserion.test_strings")
            prop(collection, "expected", expected)
            prop(assertion, "Assertion.test_field", field)
            prop(assertion, "Assertion.assume_success", True if field=="Assertion.response_code" else False, "boolProp")
            prop(assertion, "Assertion.test_type", match, "intProp")
        if spec.extract_json:
            extractor, _ = component(child, "JSONPostProcessor", "响应变量提取", "JSONPostProcessorGui")
            for key,value in [("referenceNames",";".join(spec.extract_json)),("jsonPathExprs",";".join(spec.extract_json.values())),("match_numbers",";".join("1" for _ in spec.extract_json)),("defaultValues",";".join("NOT_FOUND" for _ in spec.extract_json))]:
                prop(extractor,"JSONPostProcessor."+key,value)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")

def validate_plan(code):
    if not code.strip() or len(code.encode()) > 150000 or "<!DOCTYPE" in code.upper() or "<!ENTITY" in code.upper():
        raise ValueError("JMX 不能为空、不能超过 150KB，且不允许 DTD/实体")
    try: root = ET.fromstring(code)
    except ET.ParseError as e: raise ValueError("JMX XML 格式错误："+str(e)) from e
    if root.tag != "jmeterTestPlan" or root.find("hashTree") is None:
        raise ValueError("需要原生 JMeter jmeterTestPlan/hashTree 结构")
    groups = root.findall(".//ThreadGroup")
    if len(groups) != 1:
        raise ValueError("首期支持一个标准 ThreadGroup；阶梯/峰值通过运行配置的 stages 设置")
    if not root.findall(".//HTTPSamplerProxy"):
        raise ValueError("JMX 必须包含至少一个 HTTP 请求")
    for tree in root.iter("hashTree"):
        children = list(tree)
        if len(children)%2 or any(children[i+1].tag!="hashTree" for i in range(0,len(children),2)):
            raise ValueError("JMX 组件后必须紧跟对应的 hashTree")
    return root

# 从不可变版本生成本次执行副本，替换目标环境并应用阶段负载。
# 当前 stages 通过复制标准 ThreadGroup、设置不同延迟实现，原始用例版本保持不变。
# 移除图形监听器，实际使用非 GUI 模式生成 JTL/HTML 结果。
def materialize(code, config, target):
    """Create the execution copy. Immutable source is never modified."""
    import copy
    from urllib.parse import urlparse
    root = validate_plan(code)
    config = PerformanceConfig.model_validate(config)
    parsed = urlparse(target)
    tree = next(t for t in root.iter("hashTree") if any(c.tag=="ThreadGroup" for c in t))
    children=list(tree); i=next(i for i,c in enumerate(children) if c.tag=="ThreadGroup")
    original, child_tree=children[i:i+2]
    tree.remove(original);tree.remove(child_tree)
    stages=config.stages or [Stage(users=config.users,seconds=config.duration_seconds,ramp_seconds=config.ramp_seconds)]
    delay=0
    for index,stage in enumerate(stages):
        g, content=copy.deepcopy(original),copy.deepcopy(child_tree)
        g.set("testname",f"阶段 {index+1} · {stage.users} 用户")
        values={"ThreadGroup.num_threads":stage.users,"ThreadGroup.ramp_time":stage.ramp_seconds,"ThreadGroup.duration":stage.seconds,"ThreadGroup.delay":delay,"ThreadGroup.scheduler":True,"LoopController.loops":-1}
        for name,value in values.items():
            node=next((n for n in g.iter() if n.get("name")==name),None)
            if node is None: node=prop(g,name,value,"boolProp" if isinstance(value,bool) else "stringProp")
            else: node.text=str(value).lower() if isinstance(value,bool) else str(value)
        tree.append(g);tree.append(content)
        delay+=stage.seconds
    # Environment selection applies to every HTTP sampler, including imported plans.
    for sampler in root.iter("HTTPSamplerProxy"):
        for suffix,value in [("domain",parsed.hostname),("port",parsed.port or (443 if parsed.scheme=="https" else 80)),("protocol",parsed.scheme)]:
            name="HTTPSampler."+suffix
            node=next((n for n in sampler if n.get("name")==name),None)
            if node is None: prop(sampler,name,value)
            else: node.text=str(value)
        path=next((n for n in sampler if n.get("name")=="HTTPSampler.path"),None)
        if path is not None and parsed.path.rstrip("/") and path.text and path.text.startswith("/"):
            path.text=parsed.path.rstrip("/")+path.text
    # Drop GUI listeners from the execution copy.
    for t in root.iter("hashTree"):
        children=list(t)
        for i in range(0,len(children)-1,2):
            if children[i].tag in {"ResultCollector","BackendListener"}:
                t.remove(children[i]);t.remove(children[i+1])
    ET.indent(root)
    return ET.tostring(root,encoding="unicode")

def percentile(values,p):
    if not values:return 0
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)]

# 从 JTL 样本计算吞吐、失败率和 P95 等，忽略预热样本，并避免事务汇总与子请求重复计数。
# P95 是排序后约 95% 样本不超过的耗时，不是平均值。
# 没有配置性能阈值时返回 not_evaluated：测完了不代表性能已经达标。
def read_metrics(path, config, final=False):
    config=PerformanceConfig.model_validate(config)
    groups={};buckets={};failures=Counter();timestamps=[]
    all_times=[];failed=0;first=None;last=None;truncated=False
    transaction_labels=set()
    if path.parent.joinpath("plan.jmx").exists():
        try: transaction_labels={n.get("testname") for n in ET.parse(path.parent/"plan.jmx").iter("TransactionController")}
        except ET.ParseError: pass
    if not path.exists(): return {"samples":0,"failed":0,"errorRate":None,"rps":0,"p50":0,"p95":0,"p99":0,"average":0,"measuredSeconds":0,"truncated":False,"series":[],"endpoints":[],"thresholds":[],"failures":[]}
    with path.open(newline="",errors="replace") as f:
        for number,row in enumerate(csv.DictReader(f)):
            if number>=1000000:
                truncated=True;break
            try:
                timestamp=int(row["timeStamp"]);elapsed=float(row["elapsed"])
                label=row["label"];success=row["success"].lower()=="true"
                if not label or label in transaction_labels:continue
            except (KeyError,ValueError,AttributeError):continue
            if first is None:first=timestamp
            last=max(last or timestamp,timestamp+int(elapsed))
            if timestamp<first+config.warmup_seconds*1000:continue
            all_times.append(elapsed);failed+=not success;timestamps.append(timestamp)
            values=groups.setdefault(label,{"times":[],"failed":0})
            values["times"].append(elapsed);values["failed"]+=not success
            bucket=(timestamp-first)//2000
            b=buckets.setdefault(bucket,{"times":[],"failed":0,"users":0})
            b["times"].append(elapsed);b["failed"]+=not success
            b["users"]=max(b["users"],int(row.get("allThreads") or 0))
            if not success: failures[(label,row.get("responseCode",""),(row.get("failureMessage") or "")[:300])]+=1
    samples=len(all_times)
    observed=max(.001,((last or first or 0)-(first or 0))/1000-config.warmup_seconds)
    duration=sum(s.seconds for s in config.stages) if config.stages else config.duration_seconds
    seconds=max(observed,duration-config.warmup_seconds) if final else observed
    metrics={"truncated":truncated,"samples":samples,"failed":failed,"errorRate":failed/samples if samples else None,"rps":samples/seconds,"p50":percentile(all_times,.5),"p95":percentile(all_times,.95),"p99":percentile(all_times,.99),"average":sum(all_times)/samples if samples else 0,"measuredSeconds":round(seconds,3),
      "endpoints":[{"name":name,"samples":len(v["times"]),"failed":v["failed"],"p95":percentile(v["times"],.95),"average":sum(v["times"])/len(v["times"])} for name,v in groups.items()],
      "series":[{"second":int(k*2),"rps":len(v["times"])/2,"p95":percentile(v["times"],.95),"errorRate":v["failed"]/len(v["times"]),"users":v["users"]} for k,v in sorted(buckets.items())],
      "failures":[{"name":k[0],"code":k[1],"message":k[2],"count":v} for k,v in failures.most_common(20)]}
    checks=[{"name":"有效样本数","actual":samples,"limit":config.min_requests,"passed":samples>=config.min_requests}]
    if config.p95_ms is not None:checks.append({"name":"P95 响应时间 (ms)","actual":metrics["p95"],"limit":config.p95_ms,"passed":samples>0 and metrics["p95"]<=config.p95_ms})
    if config.max_error_rate is not None:checks.append({"name":"业务失败率","actual":metrics["errorRate"],"limit":config.max_error_rate,"passed":samples>0 and metrics["errorRate"]<=config.max_error_rate})
    metrics["thresholds"]=checks
    metrics["verdict"]="passed" if all(x["passed"] for x in checks) else "failed"
    if config.p95_ms is None and config.max_error_rate is None and samples>=config.min_requests:metrics["verdict"]="not_evaluated"
    if truncated:metrics["verdict"]="error"
    return metrics
