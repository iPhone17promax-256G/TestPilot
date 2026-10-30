# 性能可视化步骤的编译器：请求和控制块对应真正的 JMeter XML 组件。
# 与 API/Web 生成 Python 不同，这里的输出是 JMX 和步骤到 XML 行号的映射。

"""Compile editable JMeter requests and control blocks to native, mapped JMX."""
import copy
import json
import re
import xml.etree.ElementTree as ET
from .performance import build_plan, component, prop
from .step_blocks import block_links, active_step_indices


# 把编辑器中的 {{变量}} 翻译为 JMeter 的 ${变量}；本函数不读取真实运行数据。
# 功能：将可视化的 {{变量}} 编译成 JMeter 的 ${变量}，支持嵌套请求结构。
# 输入：value 为步骤中的文本、字典或列表；只转换引用语法，不读取运行时变量值。
# 输出/影响：返回转换后的结构；禁止 {{env.*}}，性能凭据应使用辅助 CSV，不写文件或发请求。
def replace(value):
    if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
    if isinstance(value,list): return [replace(v) for v in value]
    if not isinstance(value,str): return value
    if "{{env." in value: raise ValueError("性能凭据使用辅助 CSV 数据源，不能写 {{env.*}}")
    return re.sub(r"\{\{\s*(\w+)\s*\}\}",lambda m:chr(36)+"{"+m[1]+"}",value)


# 生成 JMeter IfController 使用的 Groovy 条件表达式。
# 进入 if 时保存判断结果，让 else 使用同一次判断，避免分支修改变量后两个分支都执行。
# 功能：生成 IfController 的 Groovy 条件，并可把判断结果保存供同次 else 使用。
# 输入：step 含 expression/operator/expected；snapshot 为可选内部 JMeter 变量名，由编译器按步骤 ID 生成。
# 输出/影响：返回转义好的 JMeter __groovy 表达式字符串；此处不执行 Groovy，实际运行时才读取/写入 JMeter vars。
def jmeter_condition(step, snapshot=None):
    actual="vars.get("+json.dumps(step["expression"])+")"
    value=step["expected"]
    match=re.fullmatch(r"\{\{\s*(\w+)\s*\}\}",value) if isinstance(value,str) else None
    expected="vars.get("+json.dumps(match[1])+")" if match else json.dumps(str(value).lower() if isinstance(value,bool) else str(value) if value is not None else "null",ensure_ascii=False)
    op=step["operator"]
    if op=="exists":expression=actual+" != null"
    elif op=="not_exists":expression=actual+" == null"
    elif op in {"eq","ne"}:expression=actual+(" == " if op=="eq" else " != ")+expected
    elif op in {"contains","matches"}:expression=actual+" != null && "+actual+"."+("contains" if op=="contains" else "matches")+"("+expected+")"
    else:expression=actual+" != null && new BigDecimal("+actual+") "+{"gt":">","gte":">=","lt":"<","lte":"<="}[op]+" new BigDecimal("+expected+")"
    if snapshot is not None:
        # 进入当前 if 控制块时，只计算一次条件并存入内部 JMeter 变量；if 分支的请求/提取步骤可能修改条件引用的业务变量，
        # 所以对应 else 必须读取之前保存的判断结果，不能重新计算条件，否则 if 和 else 可能在同一轮都执行。
        expression = "def matched = ("+expression+"); vars.put("+json.dumps(snapshot)+", String.valueOf(matched)); matched"
    # 生成 JMeter 函数参数时，逗号需按 JMeter 的参数分隔规则另做转义；仅转义 Groovy 字符串还不够，条件表达式可能被拆成多个函数参数。
    return chr(36)+"{__groovy("+expression.replace(",", "\\,")+")}"


# 按控制块嵌套关系构造 JMX 树，再从步骤标记计算 stepMap，供界面和报告定位。
# 功能：把可视化性能请求和嵌套控制块编译成 JMX，同时建立步骤到源码行号的映射。
# 输入：steps 为校验后的步骤字典列表，含启用状态、请求、CSV、if/else、循环和事务配置。
# 输出/影响：返回 (JMX 源码, stepMap 列表)；只改内存 XML，禁用块不产生负载，不保存版本或运行压测。
def compile_plan(steps):
    fields=["id","name","method","path","headers","params","body","body_type","timeout","follow_redirects","expected_status","body_pattern","extract_json"]
    active = active_step_indices(steps)
    requests = [replace({k:s[k] for k in fields}) for i,s in enumerate(steps) if i in active and s["action"]=="request"]
    if not requests: raise ValueError("性能计划至少需要一个启用的 HTTP 请求")
    root=ET.fromstring(build_plan(requests))
    group_tree=next(t for t in root.iter("hashTree") if any(c.tag=="HTTPSamplerProxy" for c in t))
    nodes=list(group_tree)
    samplers=[(copy.deepcopy(nodes[i]),copy.deepcopy(nodes[i+1])) for i in range(0,len(nodes),2) if nodes[i].tag=="HTTPSamplerProxy"]
    for node in nodes[4:]:group_tree.remove(node)
    sampler_index=0
    links=block_links(steps)
    # 功能：递归把一段步骤填入指定 hashTree，匹配控制块并保持请求模板顺序。
    # 输入：lo/hi 为 steps 的半开下标范围；parent 为目标 XML 节点；enabled 为父块有效状态；其余模板与 links 来自外层。
    # 输出/影响：返回 None；修改 parent 内存树及闭包 sampler_index，生成步骤标记、请求和控制器，不写文件。
    def walk(lo,hi,parent,enabled=True):
        nonlocal sampler_index
        i=lo
        while i<hi:
            s=steps[i];action=s["action"]
            # 被禁用的控制块及其子步骤不生成请求模板；这里遍历可用 HTTP 请求步骤时，要先确认它实际生成了对应模板，不能直接按原步骤下标取值。
            pair=None
            if action=="request" and i in active:
                pair=samplers[sampler_index];sampler_index+=1
            if enabled and s["enabled"]:
                parent.append(ET.Comment(" TestPilot step: "+s["id"].encode().hex()+" "))
                if action=="request":
                    sampler, child=pair;parent.append(sampler);parent.append(child)
                    for expression,expected in replace(s["assert_json"]).items():
                        assertion,_=component(child,"JSONPathAssertion","检查 JSON "+expression,"JSONPathAssertionGui")
                        for key,value,tag in [("JSON_PATH",expression,"stringProp"),("EXPECTED_VALUE",json.dumps(expected,ensure_ascii=False,separators=(",",":")),"stringProp"),("JSONVALIDATION",True,"boolProp"),("EXPECT_NULL",expected is None,"boolProp"),("INVERT",False,"boolProp"),("ISREGEX",False,"boolProp")]:prop(assertion,key,value,tag)
                    if s["max_response_ms"] is not None:
                        assertion,_=component(child,"DurationAssertion","单次请求耗时上限","DurationAssertionGui")
                        prop(assertion,"DurationAssertion.duration",s["max_response_ms"],"longProp")
                elif action=="csv":
                    csv,_=component(parent,"CSVDataSet",s["name"],"TestBeanGUI")
                    for key,value,tag in [("filename",s["value"],"stringProp"),("fileEncoding","UTF-8","stringProp"),("variableNames",s["columns"],"stringProp"),("delimiter",s["delimiter"],"stringProp"),("ignoreFirstLine",s["ignore_first_line"],"boolProp"),("quotedData",True,"boolProp"),("recycle",s["recycle"],"boolProp"),("stopThread",s["stop_thread"],"boolProp"),("shareMode","shareMode."+s["share_mode"],"stringProp")]:prop(csv,key,value,tag)
                elif action == "if":
                    end,alt=links[i]
                    snapshot = "__testpilot_if_" + s["id"].encode().hex()
                    condition=jmeter_condition(s, snapshot=snapshot)
                    controller,child=component(parent,"IfController",s["name"],"IfControllerPanel")
                    prop(controller,"IfController.condition",condition);prop(controller,"IfController.evaluateAll",False,"boolProp");prop(controller,"IfController.useExpression",True,"boolProp")
                    walk(i+1,alt if alt is not None else end,child)
                    if alt is not None:
                        parent.append(ET.Comment(" TestPilot step: "+steps[alt]["id"].encode().hex()+" "))
                        inverse=chr(36)+"{__groovy(vars.get("+json.dumps(snapshot)+") == "+json.dumps("false")+")}"
                        controller,child=component(parent,"IfController","否则 · "+s["name"],"IfControllerPanel")
                        prop(controller,"IfController.condition",inverse);prop(controller,"IfController.evaluateAll",False,"boolProp");prop(controller,"IfController.useExpression",True,"boolProp")
                        walk(alt+1,end,child)
                    parent.append(ET.Comment(" TestPilot step: "+steps[end]["id"].encode().hex()+" "));i=end
                elif i in links:
                    end,_=links[i]
                    tag={"repeat":"LoopController","once":"OnceOnlyController","transaction":"TransactionController"}[action]
                    controller,child=component(parent,tag,"事务 · "+s["name"] if action=="transaction" else s["name"],tag+"Gui")
                    if action=="repeat":
                        prop(controller,"LoopController.continue_forever",False,"boolProp");prop(controller,"LoopController.loops",s["count"],"intProp")
                    if action=="transaction":
                        prop(controller,"TransactionController.parent",False,"boolProp");prop(controller,"TransactionController.includeTimers",False,"boolProp")
                    walk(i+1,end,child);child.append(ET.Comment(" TestPilot step: "+steps[end]["id"].encode().hex()+" "));i=end
            elif i in links:
                end,_=links[i];walk(i+1,end,parent,False);i=end
            i+=1
    walk(0,len(steps),group_tree)
    ET.indent(root)
    code=ET.tostring(root,encoding="unicode")
    lines=code.splitlines();mapping=[]
    markers={s["id"]:next((i+1 for i,line in enumerate(lines) if "<!-- TestPilot step: "+s["id"].encode().hex()+" -->" in line),None) for s in steps}
    for index,s in enumerate(steps):
        start=markers[s["id"]]
        if start:
            next_marker=next((i for i in range(start,len(lines)) if "<!-- TestPilot step:" in lines[i]),None)
            end=next_marker if next_marker is not None else next(i+1 for i in range(start,len(lines)) if lines[i]=="      </hashTree>")
        else:end=None
        mapping.append(dict(id=s["id"],index=index+1,name=s["name"],start=start,end=end,enabled=bool(start)))
    return code,mapping
