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
def replace(value):
    if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
    if isinstance(value,list): return [replace(v) for v in value]
    if not isinstance(value,str): return value
    if "{{env." in value: raise ValueError("性能凭据使用辅助 CSV 数据源，不能写 {{env.*}}")
    return re.sub(r"\{\{\s*(\w+)\s*\}\}",lambda m:chr(36)+"{"+m[1]+"}",value)


# 生成 JMeter IfController 使用的 Groovy 条件表达式。
# 进入 if 时保存判断结果，让 else 使用同一次判断，避免分支修改变量后两个分支都执行。
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
        # Capture once on entry. The true branch may change the source variable;
        # its sibling else controller must use the captured decision, not read it again.
        expression = "def matched = ("+expression+"); vars.put("+json.dumps(snapshot)+", String.valueOf(matched)); matched"
    # Function arguments escape commas independently of the Groovy string literal.
    return chr(36)+"{__groovy("+expression.replace(",", "\\,")+")}"


# 按控制块嵌套关系构造 JMX 树，再从步骤标记计算 stepMap，供界面和报告定位。
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
    def walk(lo,hi,parent,enabled=True):
        nonlocal sampler_index
        i=lo
        while i<hi:
            s=steps[i];action=s["action"]
            # Templates exist only for requests outside disabled control subtrees.
            pair=None
            if action=="request" and i in active:
                pair=samplers[sampler_index];sampler_index+=1
            if enabled and s["enabled"]:
                parent.append(ET.Comment(" AIQA step: "+s["id"].encode().hex()+" "))
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
                    snapshot = "__aiqa_if_" + s["id"].encode().hex()
                    condition=jmeter_condition(s, snapshot=snapshot)
                    controller,child=component(parent,"IfController",s["name"],"IfControllerPanel")
                    prop(controller,"IfController.condition",condition);prop(controller,"IfController.evaluateAll",False,"boolProp");prop(controller,"IfController.useExpression",True,"boolProp")
                    walk(i+1,alt if alt is not None else end,child)
                    if alt is not None:
                        parent.append(ET.Comment(" AIQA step: "+steps[alt]["id"].encode().hex()+" "))
                        inverse=chr(36)+"{__groovy(vars.get("+json.dumps(snapshot)+") == "+json.dumps("false")+")}"
                        controller,child=component(parent,"IfController","否则 · "+s["name"],"IfControllerPanel")
                        prop(controller,"IfController.condition",inverse);prop(controller,"IfController.evaluateAll",False,"boolProp");prop(controller,"IfController.useExpression",True,"boolProp")
                        walk(alt+1,end,child)
                    parent.append(ET.Comment(" AIQA step: "+steps[end]["id"].encode().hex()+" "));i=end
                elif i in links:
                    end,_=links[i]
                    tag={"repeat":"LoopController","once":"OnceOnlyController","transaction":"TransactionController"}[action]
                    controller,child=component(parent,tag,"事务 · "+s["name"] if action=="transaction" else s["name"],tag+"Gui")
                    if action=="repeat":
                        prop(controller,"LoopController.continue_forever",False,"boolProp");prop(controller,"LoopController.loops",s["count"],"intProp")
                    if action=="transaction":
                        prop(controller,"TransactionController.parent",False,"boolProp");prop(controller,"TransactionController.includeTimers",False,"boolProp")
                    walk(i+1,end,child);child.append(ET.Comment(" AIQA step: "+steps[end]["id"].encode().hex()+" "));i=end
            elif i in links:
                end,_=links[i];walk(i+1,end,parent,False);i=end
            i+=1
    walk(0,len(steps),group_tree)
    ET.indent(root)
    code=ET.tostring(root,encoding="unicode")
    lines=code.splitlines();mapping=[]
    markers={s["id"]:next((i+1 for i,line in enumerate(lines) if "<!-- AIQA step: "+s["id"].encode().hex()+" -->" in line),None) for s in steps}
    for index,s in enumerate(steps):
        start=markers[s["id"]]
        if start:
            next_marker=next((i for i in range(start,len(lines)) if "<!-- AIQA step:" in lines[i]),None)
            end=next_marker if next_marker is not None else next(i+1 for i in range(start,len(lines)) if lines[i]=="      </hashTree>")
        else:end=None
        mapping.append(dict(id=s["id"],index=index+1,name=s["name"],start=start,end=end,enabled=bool(start)))
    return code,mapping
